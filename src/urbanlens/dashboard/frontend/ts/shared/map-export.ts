/**
 * Shared map-export module: rasterizes the CURRENTLY VISIBLE view of a Leaflet map.
 */
import type { MapLayersInstance } from "./map-layers";
import { MapLayers } from "./map-layers";
import type { LatLngTuple, ShapeSpec } from "./markup-engine";
import { MarkupEngine, safeColor } from "./markup-engine";
import { isOwnTileUrl, loadOwnTileImage } from "./own-tiles";

// `L` is loaded globally via a CDN <script> tag - see markup-engine.ts for
// why this is an ambient declaration rather than a bundled import.
declare const L: typeof import("leaflet");

const TILE_SIZE = 256;
const TILE_LOAD_TIMEOUT_MS = 8000;

/** Tiles fetched at once while rasterizing. Own tiles are queued more narrowly again by `own-tiles.ts`. */
const EXPORT_TILE_CONCURRENCY = 8;

export interface MapExportOptions {
    /** The MapLayers engine instance already bound to this map. */
    layers: MapLayersInstance;
    /** Currently-drawn markup shapes, in the snapshot ShapeSpec format. */
    getShapes?: () => ShapeSpec[];
    /** Download filename; defaults to a timestamp-based name. */
    filename?: string;
}

/** Resolves the actually-visible base tile key, accounting for dark mode swapping street->dark. */
function activeBaseKey(layers: MapLayersInstance): string {
    const key = layers.baseKey();
    return key === "street" && layers.isDarkActive() ? "dark" : key;
}

/**
 * Loads a tile image, resolving `null` (rather than rejecting) on error or timeout so one bad tile
 * can't hang the export.
 *
 * A tile this deployment serves itself goes through `own-tiles.ts` instead, so an export queues
 * behind - and retries alongside - the map the user is looking at, rather than racing it for the
 * proxy's few upstream slots and silently leaving holes in the file it hands back.
 */
function loadTileImage(url: string): Promise<CanvasImageSource | null> {
    if (isOwnTileUrl(url)) return loadOwnTileImage(url);
    return new Promise((resolve) => {
        let settled = false;
        const done = (result: HTMLImageElement | null) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            resolve(result);
        };
        const timer = setTimeout(() => done(null), TILE_LOAD_TIMEOUT_MS);
        const img = new Image();
        img.crossOrigin = "anonymous";
        img.onload = () => done(img);
        img.onerror = () => done(null);
        img.src = url;
    });
}

/**
 * Runs `load` over every job, at most `limit` at a time.
 *
 * An export covers the whole visible view at full resolution, which is a bigger grid than the map
 * itself draws - firing all of it at once is a burst no tile server enjoys, this deployment's own
 * least of all.
 */
async function loadWithLimit<T>(count: number, limit: number, load: (index: number) => Promise<T>): Promise<T[]> {
    const results = new Array<T>(count);
    let next = 0;
    const workers = Array.from({ length: Math.min(limit, count) }, async () => {
        for (let index = next++; index < count; index = next++) {
            results[index] = await load(index);
        }
    });
    await Promise.all(workers);
    return results;
}

/**
 * Where the export draws from: the map's centre in projected pixels at its zoom, and how far its
 * content is turned. The canvas is drawn turned about its middle and everything is placed relative to
 * the centre, so a map that has been turned exports as it is shown, and one that has not exports
 * exactly as before.
 */
export interface ExportFrame {
    width: number;
    height: number;
    zoom: number;
    center: { x: number; y: number };
    /** Clockwise turn of the map's content, in radians - leaflet-rotate's bearing. */
    turn: number;
}

/**
 * The area of the unturned map a turned view shows, as projected-pixel bounds at the frame's zoom:
 * the box around the view's rectangle turned back. Wider and taller than the view whenever it is
 * turned, so the corners have tiles under them.
 */
export function frameBounds(frame: ExportFrame): { minX: number; minY: number; maxX: number; maxY: number } {
    const cos = Math.abs(Math.cos(frame.turn));
    const sin = Math.abs(Math.sin(frame.turn));
    const halfWidth = (frame.width * cos + frame.height * sin) / 2;
    const halfHeight = (frame.width * sin + frame.height * cos) / 2;
    return { minX: frame.center.x - halfWidth, minY: frame.center.y - halfHeight, maxX: frame.center.x + halfWidth, maxY: frame.center.y + halfHeight };
}

/** The tiles covering `frame` at `fetchZoom`, with where each is drawn relative to the frame's centre. */
export function frameTiles(frame: ExportFrame, fetchZoom: number): Array<{ x: number; y: number; z: number; left: number; top: number; size: number }> {
    const size = TILE_SIZE * 2 ** (frame.zoom - fetchZoom);
    const bounds = frameBounds(frame);
    const count = 2 ** fetchZoom;
    const tiles: Array<{ x: number; y: number; z: number; left: number; top: number; size: number }> = [];
    for (let x = Math.floor(bounds.minX / size); x < Math.ceil(bounds.maxX / size); x++) {
        for (let y = Math.floor(bounds.minY / size); y < Math.ceil(bounds.maxY / size); y++) {
            // Off the top or bottom of the world there is no tile; sideways the world repeats.
            if (y < 0 || y >= count) continue;
            tiles.push({ x: ((x % count) + count) % count, y, z: fetchZoom, left: x * size - frame.center.x, top: y * size - frame.center.y, size });
        }
    }
    return tiles;
}

/** The map's own frame - its size, zoom, centre and turn. */
function mapFrame(map: L.Map): ExportFrame {
    const size = map.getSize();
    const zoom = map.getZoom();
    const center = map.project(map.getCenter(), zoom);
    const turned = map as L.Map & { getBearing?: () => number; options: { rotate?: boolean } };
    const degrees = turned.options.rotate && typeof turned.getBearing === "function" ? turned.getBearing() : 0;
    return { width: size.x, height: size.y, zoom, center: { x: center.x, y: center.y }, turn: (degrees * Math.PI) / 180 };
}

/** Draws every tile of `tileLayer` the frame shows onto `ctx`, which is already turned and centred. */
async function drawTileLayerGrid(ctx: CanvasRenderingContext2D, frame: ExportFrame, tileLayer: L.TileLayer): Promise<void> {
    // Clamp to the provider's real tile depth (mirrors Leaflet's own
    // maxNativeZoom upscaling) and fetch coarser tiles, drawn larger, past it.
    const maxNative = tileLayer.options.maxNativeZoom;
    const tileZoom = Math.round(frame.zoom);
    const fetchZoom = typeof maxNative === "number" ? Math.min(tileZoom, maxNative) : tileZoom;
    const jobs = frameTiles(frame, fetchZoom);
    if (!jobs.length) return;

    // TileLayer.getTileUrl() ignores the .z on the coords it's passed and instead reads its own
    // private _tileZoom, which Leaflet only sets while the layer is on a map.
    (tileLayer as unknown as { _tileZoom: number })._tileZoom = fetchZoom;

    // Every URL is resolved before the first await, because getTileUrl() reads the _tileZoom set
    // above and anything else touching this layer meanwhile would move it.
    // getTileUrl() expects an L.Coords (a real Point plus .z) - build one from
    // an actual L.point() rather than a plain object literal.
    const urls = jobs.map((job) => {
        const coords = L.point(job.x, job.y) as L.Coords;
        coords.z = job.z;
        return tileLayer.getTileUrl(coords);
    });

    const images = await loadWithLimit(jobs.length, EXPORT_TILE_CONCURRENCY, (index) => loadTileImage(urls[index]!).catch(() => null));

    jobs.forEach((job, i) => {
        const img = images[i];
        // A hair over size, so the seams between tiles do not show as hairlines once turned.
        const overlap = frame.turn ? 0.5 : 0;
        if (img) ctx.drawImage(img, job.left, job.top, job.size + overlap, job.size + overlap);
    });
}

/** Draws a rotated arrowhead triangle at `tip`, matching MarkupEngine.arrowheadSvg's geometry. */
function drawArrowhead(ctx: CanvasRenderingContext2D, tip: Pt, deg: number, color: string, size: number, opacity: number): void {
    const rad = (deg * Math.PI) / 180;
    const tipLen = size * 0.43;
    const bx = size * 0.36;
    const by = size * 0.29;
    const local: LatLngTuple[] = [
        [0, -tipLen],
        [bx, by],
        [-bx, by],
    ];
    const cos = Math.cos(rad);
    const sin = Math.sin(rad);
    ctx.beginPath();
    local.forEach(([lx, ly], i) => {
        const x = tip.x + lx * cos - ly * sin;
        const y = tip.y + lx * sin + ly * cos;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    });
    ctx.closePath();
    ctx.globalAlpha = opacity;
    ctx.fillStyle = color;
    ctx.fill();
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 1.5;
    ctx.lineJoin = "round";
    ctx.stroke();
    ctx.globalAlpha = 1;
}

interface Pt {
    x: number;
    y: number;
}

/**
 * Draws one markup shape (already in ShapeSpec/snapshot format) onto the canvas. Approximates, rather
 * than pixel-matches, the DOM/SVG renderer - acceptable for a downloaded reference image.
 *
 * `ctx` is turned with the map. Paths turn with it, as they do on screen; a label and a pin are
 * markers on screen and stay upright, so they are turned back before they are drawn.
 */
function drawShape(ctx: CanvasRenderingContext2D, toPoint: (ll: LatLngTuple) => Pt, s: ShapeSpec, turn: number): void {
    const color = safeColor(s.color, "#e74c3c");
    const weight = s.stroke_width ?? s.weight ?? 3;
    const fillOpacity = (s.fill_opacity ?? 87) / 100;
    const borderOpacity = (s.border_opacity ?? 100) / 100;
    const hasBorder = !!(s.border_color && s.border_color !== "none");
    const strokeColor = hasBorder ? safeColor(s.border_color, color) : color;
    // As `renderShape` draws them: a shape with no border colour gets a 2px outline in its fill colour.
    const outlineWidth = hasBorder ? weight : 2;

    function strokePath(pts: Pt[], close: boolean): void {
        ctx.beginPath();
        pts.forEach((p, i) => (i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y)));
        if (close) ctx.closePath();
    }

    function fillAndOutline(): void {
        ctx.fillStyle = color;
        ctx.globalAlpha = fillOpacity;
        ctx.fill();
        ctx.strokeStyle = strokeColor;
        ctx.lineWidth = outlineWidth;
        ctx.lineJoin = "round";
        ctx.globalAlpha = borderOpacity;
        ctx.stroke();
        ctx.globalAlpha = 1;
    }

    /** Draws `paint` upright at `at`, however the map is turned. */
    function upright(at: Pt, paint: () => void): void {
        ctx.save();
        ctx.translate(at.x, at.y);
        ctx.rotate(-turn);
        paint();
        ctx.restore();
    }

    switch (s.type) {
        case "line":
        case "arrow": {
            const pts = s.latlngs.map(toPoint);
            strokePath(pts, false);
            ctx.strokeStyle = color;
            ctx.lineWidth = weight;
            ctx.lineJoin = "round";
            ctx.lineCap = "round";
            ctx.globalAlpha = fillOpacity;
            ctx.stroke();
            ctx.globalAlpha = 1;
            if (s.type === "arrow" && pts.length >= 2) {
                const n = pts.length;
                const a = pts[n - 2]!;
                const b = pts[n - 1]!;
                // The angle along the line as drawn, which turns with the canvas.
                const deg = (Math.atan2(b.x - a.x, a.y - b.y) * 180) / Math.PI;
                drawArrowhead(ctx, b, deg, color, MarkupEngine.arrowheadSize(), fillOpacity);
            }
            break;
        }
        case "circle": {
            const c = toPoint(s.latlngs[0]!);
            const e = toPoint(s.latlngs[1]!);
            ctx.beginPath();
            ctx.arc(c.x, c.y, Math.hypot(e.x - c.x, e.y - c.y), 0, Math.PI * 2);
            fillAndOutline();
            break;
        }
        case "rect": {
            const [a, b] = s.latlngs as [LatLngTuple, LatLngTuple];
            strokePath([toPoint(a), toPoint([a[0], b[1]]), toPoint(b), toPoint([b[0], a[1]])], true);
            fillAndOutline();
            break;
        }
        case "polygon": {
            strokePath(s.latlngs.map(toPoint), true);
            fillAndOutline();
            break;
        }
        case "text": {
            const fontSize = Math.max(8, Math.min(96, weight || 16));
            const label = s.label || "";
            ctx.font = `600 ${fontSize}px sans-serif`;
            // As textLabelHtml lays it out: .15em .45em of padding, a 1.3 line height.
            const paddingX = fontSize * 0.45;
            const boxW = ctx.measureText(label).width + paddingX * 2;
            const boxH = fontSize * 1.3 + fontSize * 0.3;
            const background = s.border_color === "none" ? null : hasBorder ? strokeColor : "rgba(255,255,255,0.92)";
            const textTurn = (MarkupEngine.textRotation(s) * Math.PI) / 180;
            upright(toPoint(s.latlngs[0]!), () => {
                // The label is anchored by its top-left corner and turned about its middle.
                ctx.translate(boxW / 2, boxH / 2);
                ctx.rotate(textTurn);
                if (background) {
                    ctx.fillStyle = background;
                    ctx.fillRect(-boxW / 2, -boxH / 2, boxW, boxH);
                }
                ctx.fillStyle = color;
                ctx.textBaseline = "middle";
                ctx.fillText(label, -boxW / 2 + paddingX, 0);
            });
            break;
        }
        case "pin": {
            upright(toPoint(s.latlngs[0]!), () => {
                const r = 9;
                ctx.beginPath();
                ctx.arc(0, -r - 4, r, 0, Math.PI * 2);
                ctx.fillStyle = color;
                ctx.globalAlpha = 1;
                ctx.fill();
                ctx.strokeStyle = "rgba(0,0,0,.35)";
                ctx.lineWidth = 1;
                ctx.stroke();
                ctx.beginPath();
                ctx.moveTo(-r * 0.6, -r * 0.6);
                ctx.lineTo(r * 0.6, -r * 0.6);
                ctx.lineTo(0, 0);
                ctx.closePath();
                ctx.fill();
            });
            break;
        }
    }
}

/**
 * Splits `text` into lines no wider than `maxWidth`, breaking between words. Nothing is cut short to
 * fit: a credit is owed in full, so a word wider than the line gets a line of its own.
 */
export function wrapCredit(text: string, maxWidth: number, measure: (line: string) => number): string[] {
    const lines: string[] = [];
    let line = "";
    for (const word of text.split(" ")) {
        const next = line ? `${line} ${word}` : word;
        if (line && measure(next) > maxWidth) {
            lines.push(line);
            line = word;
        } else {
            line = next;
        }
    }
    if (line) lines.push(line);
    return lines;
}

/**
 * Writes the tiles' credit into the image's bottom-right corner. An exported image leaves the site,
 * and the credit the map shows on screen is owed wherever the picture of it goes.
 */
function drawCredit(ctx: CanvasRenderingContext2D, text: string, width: number, height: number): void {
    if (!text) return;
    const fontSize = 11;
    const padX = 6;
    const padY = 3;
    const lineHeight = fontSize + 3;
    ctx.save();
    ctx.font = `${fontSize}px "Helvetica Neue", Helvetica, Arial, sans-serif`;
    const lines = wrapCredit(text, width - padX * 2 - 8, (line) => ctx.measureText(line).width);
    const boxW = Math.min(width, Math.max(...lines.map((line) => ctx.measureText(line).width)) + padX * 2);
    const boxH = lines.length * lineHeight + padY * 2;
    // Esri's recommended treatment: #323232 on white at 65% or more.
    ctx.fillStyle = "rgba(255,255,255,0.8)";
    ctx.fillRect(width - boxW, height - boxH, boxW, boxH);
    ctx.fillStyle = "#323232";
    ctx.textBaseline = "top";
    lines.forEach((line, i) => ctx.fillText(line, width - boxW + padX, height - boxH + padY + i * lineHeight + 1));
    ctx.restore();
}

export const MapExport = {
    /**
     * Rasterizes `map`'s current view - turned as it is shown - to a JPEG with the tiles' credit in
     * its corner, and triggers a browser download. Single click, no confirmation step.
     */
    async download(map: L.Map, options: MapExportOptions): Promise<void> {
        const frame = mapFrame(map);
        const canvas = document.createElement("canvas");
        canvas.width = frame.width;
        canvas.height = frame.height;
        const ctx = canvas.getContext("2d");
        if (!ctx) return;

        // White background first - JPEG has no alpha channel, so a failed
        // tile or the map edge at low zoom would otherwise show through black.
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, canvas.width, canvas.height);

        ctx.save();
        ctx.translate(frame.width / 2, frame.height / 2);
        ctx.rotate(frame.turn);
        await drawTileLayerGrid(ctx, frame, MapLayers.tileLayer(activeBaseKey(options.layers)));
        if (options.layers.getState().borders) {
            await drawTileLayerGrid(ctx, frame, MapLayers.bordersOverlay());
        }

        const toPoint = (ll: LatLngTuple): Pt => {
            const p = map.project(L.latLng(ll[0], ll[1]), frame.zoom);
            return { x: p.x - frame.center.x, y: p.y - frame.center.y };
        };
        const shapes = options.getShapes ? options.getShapes() : [];
        shapes.forEach((s) => drawShape(ctx, toPoint, s, frame.turn));
        ctx.restore();

        drawCredit(ctx, options.layers.attribution?.(false) ?? "", frame.width, frame.height);

        const filename = options.filename || `map-${Date.now()}.jpg`;
        await new Promise<void>((resolve) => {
            canvas.toBlob(
                (blob) => {
                    if (blob) {
                        const a = document.createElement("a");
                        a.href = URL.createObjectURL(blob);
                        a.download = filename;
                        document.body.appendChild(a);
                        a.click();
                        a.remove();
                        setTimeout(() => URL.revokeObjectURL(a.href), 1000);
                    }
                    resolve();
                },
                "image/jpeg",
                0.92,
            );
        });
    },
};

export function installGlobalMapExport(): void {
    window.MapExport = MapExport;
}

declare global {
    interface Window {
        MapExport: typeof MapExport;
    }
}
