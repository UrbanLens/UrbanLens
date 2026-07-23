/**
 * Shared map-export module: rasterizes the CURRENTLY VISIBLE view of a
 * Leaflet map (active base tile layer + borders overlay, if on + drawn
 * markup shapes) onto a canvas sized to the map's actual on-screen container
 * dimensions, then triggers a one-click JPEG download. Used everywhere a
 * markup map is viewed or edited: the standalone map composer, the read-only
 * viewer dialog, and the safety check-in route map.
 *
 * All four tile providers this site uses (OSM, CARTO, OpenTopoMap, Esri
 * ArcGIS) send `Access-Control-Allow-Origin: *`, so tiles can be fetched with
 * `crossOrigin = "anonymous"` and drawn onto the canvas without tainting it -
 * no third-party screenshot library needed.
 */
import type { MapLayersInstance } from "./map-layers";
import { MapLayers } from "./map-layers";
import type { LatLngTuple, ShapeSpec } from "./markup-engine";
import { MarkupEngine } from "./markup-engine";

// `L` is loaded globally via a CDN <script> tag - see markup-engine.ts for
// why this is an ambient declaration rather than a bundled import.
declare const L: typeof import("leaflet");

const TILE_SIZE = 256;
const TILE_LOAD_TIMEOUT_MS = 8000;

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

/** Loads a tile image, resolving `null` (rather than rejecting) on error or timeout so one bad tile can't hang the export. */
function loadTileImage(url: string): Promise<HTMLImageElement | null> {
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

/** Draws every tile of `tileLayer` covering the map's current visible bounds at `zoom` onto `ctx`. */
async function drawTileLayerGrid(ctx: CanvasRenderingContext2D, map: L.Map, tileLayer: L.TileLayer, zoom: number): Promise<void> {
    // Clamp to the provider's real tile depth (mirrors Leaflet's own
    // maxNativeZoom upscaling) and fetch coarser tiles, drawn larger, past it.
    const maxNative = tileLayer.options.maxNativeZoom;
    const fetchZoom = typeof maxNative === "number" ? Math.min(zoom, maxNative) : zoom;
    const drawSize = TILE_SIZE * 2 ** (zoom - fetchZoom);

    // TileLayer.getTileUrl() ignores the .z on the coords it's passed and
    // instead reads its own private _tileZoom, which Leaflet only sets when a
    // layer is added to a map (onAdd -> _setView). This layer is a disposable
    // instance created solely for export and never attached, so _tileZoom
    // would otherwise be undefined, producing a broken URL for every tile.
    (tileLayer as unknown as { _tileZoom: number })._tileZoom = fetchZoom;

    const bounds = map.getBounds();
    const nw = map.project(bounds.getNorthWest(), fetchZoom).divideBy(TILE_SIZE).floor();
    const se = map.project(bounds.getSouthEast(), fetchZoom).divideBy(TILE_SIZE).ceil();

    const jobs: { x: number; y: number; point: L.Point }[] = [];
    for (let x = nw.x; x < se.x; x++) {
        for (let y = nw.y; y < se.y; y++) {
            const worldPoint = L.point(x, y).multiplyBy(TILE_SIZE);
            const point = map.latLngToContainerPoint(map.unproject(worldPoint, fetchZoom));
            jobs.push({ x, y, point });
        }
    }
    if (!jobs.length) return;

    // getTileUrl() expects an L.Coords (a real Point plus .z) - build one from
    // an actual L.point() rather than a plain object literal.
    const images = await Promise.allSettled(
        jobs.map((job) => {
            const coords = L.point(job.x, job.y) as L.Coords;
            coords.z = fetchZoom;
            return loadTileImage(tileLayer.getTileUrl(coords));
        }),
    );

    jobs.forEach((job, i) => {
        const result = images[i];
        const img = result && result.status === "fulfilled" ? result.value : null;
        if (img) ctx.drawImage(img, job.point.x, job.point.y, drawSize, drawSize);
    });
}

function toContainerPoint(map: L.Map, ll: LatLngTuple): L.Point {
    return map.latLngToContainerPoint(L.latLng(ll[0], ll[1]));
}

/** Draws a rotated arrowhead triangle at `tip`, matching MarkupEngine.arrowheadSvg's geometry. */
function drawArrowhead(ctx: CanvasRenderingContext2D, tip: L.Point, deg: number, color: string, size: number, opacity: number): void {
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
    ctx.globalAlpha = 1;
}

/** Draws one markup shape (already in ShapeSpec/snapshot format) onto the canvas. Approximates, rather than pixel-matches, the DOM/SVG renderer - acceptable for a downloaded reference image. */
function drawShape(ctx: CanvasRenderingContext2D, map: L.Map, s: ShapeSpec, zoom: number): void {
    const color = s.color || "#e74c3c";
    const weight = s.stroke_width ?? s.weight ?? 3;
    const fillOpacity = (s.fill_opacity ?? 87) / 100;
    const borderOpacity = (s.border_opacity ?? 100) / 100;
    const hasBorder = !!(s.border_color && s.border_color !== "none");
    const strokeColor = hasBorder ? s.border_color! : color;

    function strokePath(pts: L.Point[], close: boolean): void {
        ctx.beginPath();
        pts.forEach((p, i) => (i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y)));
        if (close) ctx.closePath();
    }

    switch (s.type) {
        case "line":
        case "arrow": {
            const pts = s.latlngs.map((ll) => toContainerPoint(map, ll));
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
                const deg = MarkupEngine.bearing(s.latlngs[n - 2]!, s.latlngs[n - 1]!);
                drawArrowhead(ctx, pts[n - 1]!, deg, color, MarkupEngine.arrowheadSize(zoom), fillOpacity);
            }
            break;
        }
        case "circle": {
            const c = toContainerPoint(map, s.latlngs[0]!);
            const e = toContainerPoint(map, s.latlngs[1]!);
            const r = Math.hypot(e.x - c.x, e.y - c.y);
            ctx.beginPath();
            ctx.arc(c.x, c.y, r, 0, Math.PI * 2);
            ctx.fillStyle = color;
            ctx.globalAlpha = fillOpacity;
            ctx.fill();
            if (hasBorder) {
                ctx.strokeStyle = strokeColor;
                ctx.lineWidth = weight;
                ctx.globalAlpha = borderOpacity;
                ctx.stroke();
            }
            ctx.globalAlpha = 1;
            break;
        }
        case "rect": {
            const p1 = toContainerPoint(map, s.latlngs[0]!);
            const p2 = toContainerPoint(map, s.latlngs[1]!);
            const x = Math.min(p1.x, p2.x);
            const y = Math.min(p1.y, p2.y);
            const w = Math.abs(p2.x - p1.x);
            const h = Math.abs(p2.y - p1.y);
            ctx.beginPath();
            ctx.rect(x, y, w, h);
            ctx.fillStyle = color;
            ctx.globalAlpha = fillOpacity;
            ctx.fill();
            if (hasBorder) {
                ctx.strokeStyle = strokeColor;
                ctx.lineWidth = weight;
                ctx.globalAlpha = borderOpacity;
                ctx.stroke();
            }
            ctx.globalAlpha = 1;
            break;
        }
        case "polygon": {
            const pts = s.latlngs.map((ll) => toContainerPoint(map, ll));
            strokePath(pts, true);
            ctx.fillStyle = color;
            ctx.globalAlpha = fillOpacity;
            ctx.fill();
            if (hasBorder) {
                ctx.strokeStyle = strokeColor;
                ctx.lineWidth = weight;
                ctx.globalAlpha = borderOpacity;
                ctx.stroke();
            }
            ctx.globalAlpha = 1;
            break;
        }
        case "text": {
            const p = toContainerPoint(map, s.latlngs[0]!);
            const fontSize = Math.max(8, Math.min(96, weight || 16));
            ctx.font = `600 ${fontSize}px sans-serif`;
            const label = s.label || "";
            const paddingX = fontSize * 0.35;
            const paddingY = fontSize * 0.25;
            const boxW = ctx.measureText(label).width + paddingX * 2;
            const boxH = fontSize + paddingY * 2;
            if (!hasBorder || s.border_color !== "none") {
                ctx.fillStyle = hasBorder ? strokeColor : "rgba(255,255,255,0.92)";
                ctx.fillRect(p.x, p.y, boxW, boxH);
            }
            ctx.fillStyle = color;
            ctx.textBaseline = "middle";
            ctx.fillText(label, p.x + paddingX, p.y + boxH / 2);
            break;
        }
        case "pin": {
            const p = toContainerPoint(map, s.latlngs[0]!);
            const r = 9;
            ctx.beginPath();
            ctx.arc(p.x, p.y - r, r, 0, Math.PI * 2);
            ctx.fillStyle = color;
            ctx.globalAlpha = 1;
            ctx.fill();
            ctx.strokeStyle = "rgba(0,0,0,.35)";
            ctx.lineWidth = 1;
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(p.x - r * 0.5, p.y - r * 0.3);
            ctx.lineTo(p.x + r * 0.5, p.y - r * 0.3);
            ctx.lineTo(p.x, p.y);
            ctx.closePath();
            ctx.fill();
            break;
        }
    }
}

export const MapExport = {
    /**
     * Rasterizes `map`'s current view to a JPEG and triggers a browser
     * download - single click, no confirmation step.
     */
    async download(map: L.Map, options: MapExportOptions): Promise<void> {
        const size = map.getSize();
        const canvas = document.createElement("canvas");
        canvas.width = size.x;
        canvas.height = size.y;
        const ctx = canvas.getContext("2d");
        if (!ctx) return;

        // White background first - JPEG has no alpha channel, so a failed
        // tile or the map edge at low zoom would otherwise show through black.
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, canvas.width, canvas.height);

        const zoom = Math.round(map.getZoom());
        await drawTileLayerGrid(ctx, map, MapLayers.tileLayer(activeBaseKey(options.layers)), zoom);
        if (options.layers.getState().borders) {
            await drawTileLayerGrid(ctx, map, MapLayers.bordersOverlay(), zoom);
        }

        const shapes = options.getShapes ? options.getShapes() : [];
        shapes.forEach((s) => drawShape(ctx, map, s, zoom));

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
