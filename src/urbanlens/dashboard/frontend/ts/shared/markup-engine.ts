/**
 * Shared Markup Engine: geometry helpers + the draw-session factory used by
 * the pin-detail/wiki map annotations toolbar and the safety check-in map's
 * destination-marker drawing. Loaded globally (like LocationSearchEngine)
 * since several independent pages instantiate a draw session against their
 * own Leaflet map instance.
 */

// `L` is loaded globally via a CDN <script> tag on pages that use this module
// (never bundled here) - this local ambient declaration only supplies types
// for it. A plain `import "leaflet"` would make bun bundle a second, separate
// copy of the Leaflet runtime into this chunk instead of reusing the one
// already on window.
declare const L: typeof import("leaflet");

export type LatLngTuple = [number, number];

export interface ShapeSpec {
    type: "line" | "arrow" | "circle" | "rect" | "polygon" | "text" | "pin";
    latlngs: LatLngTuple[];
    color?: string;
    stroke_width?: number;
    weight?: number;
    fill_opacity?: number;
    border_opacity?: number;
    border_color?: string;
    label?: string;
}

const HEX_COLOR_RE = /^#[0-9a-fA-F]{6}$/;

function safeColor(v: unknown, fallback = "#e74c3c"): string {
    return typeof v === "string" && HEX_COLOR_RE.test(v) ? v : fallback;
}

function safeOptionalColor(v: unknown, fallback = "#e74c3c"): string {
    if (v === "none") return "none";
    return safeColor(v, fallback);
}

function safeNumber(v: unknown, lo: number, hi: number, def: number): number {
    const n = Number.parseFloat(v as string);
    if (Number.isNaN(n)) return def;
    return Math.max(lo, Math.min(hi, n));
}

function bearing(from: LatLngTuple | { lat: number; lng: number }, to: LatLngTuple | { lat: number; lng: number }): number {
    const flat = Array.isArray(from) ? from[0] : from.lat;
    const flng = Array.isArray(from) ? from[1] : from.lng;
    const tlat = Array.isArray(to) ? to[0] : to.lat;
    const tlng = Array.isArray(to) ? to[1] : to.lng;
    return Math.atan2(tlng - flng, tlat - flat) * (180 / Math.PI);
}

function arrowheadSvg(color: string, deg: number, sz = 28, opacity: number | null = 1): string {
    const op = opacity == null ? 1 : +opacity;
    const h = sz / 2;
    const tip = -(sz * 0.43);
    const bx = sz * 0.36;
    const by = sz * 0.29;
    return (
        `<svg xmlns="http://www.w3.org/2000/svg" width="${sz}" height="${sz}"`
        + ` viewBox="${-h} ${-h} ${sz} ${sz}"`
        + ` style="transform:rotate(${deg.toFixed(1)}deg);opacity:${op.toFixed(2)}">`
        + `<polygon points="0,${tip.toFixed(1)} ${bx.toFixed(1)},${by.toFixed(1)} ${(-bx).toFixed(1)},${by.toFixed(1)}"`
        + ` fill="${color}" stroke="white" stroke-width="1.5" stroke-linejoin="round"/></svg>`
    );
}

function arrowheadSize(zoom?: number | null): number {
    if (zoom == null || zoom >= 16) return 28;
    if (zoom >= 13) return 20;
    if (zoom >= 10) return 14;
    return 8;
}

function textLabelHtml(s: ShapeSpec): string {
    const color = safeColor(s.color, "#e53e3e");
    const sz = safeNumber(s.stroke_width, 8, 96, 16);
    const bg = s.border_color;
    const bgVal = !bg || bg === "none" ? "rgba(255,255,255,0.92)" : safeColor(bg, "rgba(255,255,255,0.92)" as never);
    const lbl = String(s.label ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
    return (
        `<span class="markup-text-label" style="color:${color}`
        + `;font-size:${sz}px;background:${bgVal}`
        + ";display:inline-block;padding:.15em .45em;border-radius:3px"
        + ";white-space:nowrap;line-height:1.3;font-weight:600"
        + `;box-shadow:0 1px 3px rgba(0,0,0,.2)">${lbl || "&nbsp;"}</span>`
    );
}

/** Renders a stored shape spec into a Leaflet layer group (comment-composer + detail/wiki views). */
function renderShape(s: ShapeSpec, group: L.LayerGroup, zoom?: number): void {
    if (typeof L === "undefined") return;
    const color = safeColor(s.color, "#e74c3c");
    const weight = safeNumber(s.stroke_width != null ? s.stroke_width : s.weight, 1, 50, 3);
    const fillOp = safeNumber(s.fill_opacity != null ? s.fill_opacity : 87, 0, 100, 87) / 100;
    const borderOp = safeNumber(s.border_opacity != null ? s.border_opacity : 100, 0, 100, 100) / 100;
    const bc = s.border_color && s.border_color !== "none" ? safeColor(s.border_color, color) : null;
    const hasBorder = !!bc;
    const strokeC = hasBorder ? bc! : color;

    function shapeOpts(): L.PathOptions {
        return { color: strokeC, weight: hasBorder ? weight : 2, fillColor: color, fillOpacity: fillOp, opacity: borderOp };
    }

    switch (s.type) {
        case "line":
            L.polyline(s.latlngs, { color, weight, opacity: fillOp }).addTo(group);
            break;
        case "arrow": {
            L.polyline(s.latlngs, { color, weight, opacity: fillOp }).addTo(group);
            if (s.latlngs.length >= 2) {
                const n = s.latlngs.length;
                const deg = bearing(s.latlngs[n - 2]!, s.latlngs[n - 1]!);
                const sz2 = arrowheadSize(zoom);
                L.marker(L.latLng(s.latlngs[n - 1]![0], s.latlngs[n - 1]![1]), {
                    icon: L.divIcon({ className: "", html: arrowheadSvg(color, deg, sz2, fillOp), iconSize: [sz2, sz2], iconAnchor: [sz2 / 2, sz2 / 2] }),
                    interactive: false,
                }).addTo(group);
            }
            break;
        }
        case "circle": {
            const p1 = L.latLng(s.latlngs[0]!);
            const p2 = L.latLng(s.latlngs[1]!);
            L.circle(p1, { ...shapeOpts(), radius: p1.distanceTo(p2) }).addTo(group);
            break;
        }
        case "rect":
            L.rectangle(L.latLngBounds(s.latlngs[0]!, s.latlngs[1]!), shapeOpts()).addTo(group);
            break;
        case "polygon":
            L.polygon(s.latlngs, shapeOpts()).addTo(group);
            break;
        case "text":
            L.marker(L.latLng(s.latlngs[0]![0], s.latlngs[0]![1]), {
                icon: L.divIcon({ className: "", html: textLabelHtml(s), iconSize: undefined, iconAnchor: [0, 0] }),
                interactive: false,
            }).addTo(group);
            break;
        case "pin": {
            const sz = 32;
            const html = `<span class="material-symbols-outlined" style="font-size:${sz}px;color:${color};text-shadow:0 1px 3px rgba(0,0,0,.4)">location_on</span>`;
            L.marker(L.latLng(s.latlngs[0]![0], s.latlngs[0]![1]), {
                icon: L.divIcon({ className: "", html, iconSize: [sz, sz], iconAnchor: [sz / 2, sz] }),
                interactive: false,
            }).addTo(group);
            break;
        }
    }
}

export interface DrawSessionOpts {
    getColor?: () => string;
    getWidth?: () => number;
    getTextLabel?: () => string;
    onCommit?: (type: string, latlngs: LatLngTuple[], extras: Record<string, unknown>) => void;
    onHintChange?: (text: string) => void;
    onToolChange?: (tool: string | null) => void;
    onPointCountChange?: (tool: string | null, count: number) => void;
}

export interface DrawSession {
    startTool: (type: string) => void;
    deactivate: () => void;
    cancelShape: () => void;
    getCurrentTool: () => string | null;
    isBusy: () => boolean;
    canFinish: () => boolean;
    finishCurrent: () => void;
    destroy: () => void;
}

interface DrawState {
    points: LatLngTuple[];
}

/**
 * Draw session factory. `opts` drives styling/labels/callbacks; the session
 * owns click/drag/keyboard wiring against the given Leaflet map instance.
 */
function createDrawSession(map: L.Map, opts: DrawSessionOpts): DrawSession {
    let tool: string | null = null;
    let state: DrawState | null = null;
    const prevLayer = L.layerGroup().addTo(map);
    let lastCursorLL: L.LatLng | null = null;
    let suppressClickUntil = 0;

    const getColor = () => opts.getColor?.() ?? "#e74c3c";
    const getLabel = () => opts.getTextLabel?.() ?? "";

    function hint(): void {
        if (opts.onHintChange) {
            if (!tool) {
                opts.onHintChange("");
            } else {
                const n = state ? state.points.length : 0;
                const msgs: Record<string, string> = {
                    arrow: n >= 2 ? "Click near last point (or Enter) to finish, or drag" : n ? `${n} pt - click to add another point` : "Click to start, drag for a quick arrow",
                    line: n >= 2 ? "Click near last point (or Enter) to finish, or drag" : n ? `${n} pt - click to add another point` : "Click to start, drag for a quick line",
                    polygon: n >= 3 ? "Click near start (or Enter) to close" : n ? "Click to add vertices" : "Click to place first vertex",
                    circle: n ? "Click to set radius, or drag" : "Click to place center, or drag",
                    rect: n ? "Click second corner, or drag" : "Click first corner, or drag",
                    text: "Click to place, or drag to size a text box",
                };
                opts.onHintChange(msgs[tool] ?? "");
            }
        }
        opts.onPointCountChange?.(tool, state ? state.points.length : 0);
    }

    function clearPrev(): void {
        prevLayer.clearLayers();
    }

    function preview(cursorLL: L.LatLng): void {
        clearPrev();
        const c = getColor();

        if ((tool === "line" || tool === "arrow") && state) {
            const pts: LatLngTuple[] = [...state.points, [cursorLL.lat, cursorLL.lng]];
            L.polyline(pts, { color: c, dashArray: "5 7", weight: 2, opacity: 0.7, interactive: false }).addTo(prevLayer);
            if (tool === "arrow" && pts.length >= 2) {
                const n = pts.length;
                const deg = bearing(pts[n - 2]!, pts[n - 1]!);
                const sz = 20;
                L.marker(L.latLng(pts[n - 1]![0], pts[n - 1]![1]), {
                    icon: L.divIcon({ className: "", html: arrowheadSvg(c, deg, sz), iconSize: [sz, sz], iconAnchor: [sz / 2, sz / 2] }),
                    interactive: false,
                }).addTo(prevLayer);
            }
        } else if (tool === "polygon" && state) {
            const ppts: LatLngTuple[] = [...state.points, [cursorLL.lat, cursorLL.lng]];
            L.polygon(ppts, { color: c, dashArray: "5 7", weight: 2, fillOpacity: 0.07, interactive: false }).addTo(prevLayer);
            if (state.points.length >= 3) {
                L.circleMarker(L.latLng(state.points[0]![0], state.points[0]![1]), { radius: 8, color: c, fillColor: c, fillOpacity: 0.35, weight: 2, interactive: false }).addTo(prevLayer);
            }
        } else if (tool === "rect" && state && state.points.length >= 1) {
            L.rectangle(L.latLngBounds(state.points[0]!, [cursorLL.lat, cursorLL.lng]), { color: c, weight: 2, fillOpacity: 0.08, dashArray: "4 4", interactive: false }).addTo(prevLayer);
        } else if (tool === "circle" && state && state.points.length >= 1) {
            const center = L.latLng(state.points[0]!);
            L.circle(center, { radius: center.distanceTo(cursorLL), color: c, weight: 2, fillOpacity: 0.08, dashArray: "5 7", interactive: false }).addTo(prevLayer);
        }
    }

    function commit(type: string, latlngs: LatLngTuple[], extras?: Record<string, unknown>): void {
        clearPrev();
        state = null;
        hint();
        opts.onCommit?.(type, latlngs, extras ?? {});
    }

    function cancelShape(): void {
        state = null;
        clearPrev();
        hint();
    }

    function deactivate(): void {
        cancelShape();
        tool = null;
        map.doubleClickZoom.enable();
        map.dragging.enable();
        map.getContainer().style.cursor = "";
        opts.onToolChange?.(null);
    }

    function startTool(type: string): void {
        cancelShape();
        tool = type;
        // Disabling map panning while a tool is armed is what makes drag-to-draw
        // reliable: without this, Leaflet's own panning races the shape-drawing
        // drag on the same gesture, and since panning keeps the ground point
        // under the cursor fixed, the start/end coordinates the draw session
        // records end up nearly identical - producing "0-length" shapes.
        map.doubleClickZoom.disable();
        map.dragging.disable();
        map.getContainer().style.cursor = "crosshair";
        opts.onToolChange?.(type);
        hint();
    }

    function getCurrentTool(): string | null {
        return tool;
    }

    function isBusy(): boolean {
        return !!tool || Date.now() < suppressClickUntil;
    }

    function canFinish(): boolean {
        if (!state) return false;
        const n = state.points.length;
        if (tool === "polygon") return n >= 3;
        if (tool === "line" || tool === "arrow") return n >= 2;
        if (tool === "rect" || tool === "circle") return n >= 1 && !!lastCursorLL;
        return false;
    }

    function finishCurrent(): void {
        if (!state) return;
        const n = state.points.length;
        if (tool === "polygon" && n >= 3) {
            commit("polygon", state.points.slice());
            return;
        }
        if ((tool === "line" || tool === "arrow") && n >= 2) {
            commit(tool, state.points.slice());
            return;
        }
        if (tool === "rect" && n >= 1 && lastCursorLL) {
            commit("rect", [state.points[0]!, [lastCursorLL.lat, lastCursorLL.lng]]);
            return;
        }
        if (tool === "circle" && n >= 1 && lastCursorLL) {
            commit("circle", [state.points[0]!, [lastCursorLL.lat, lastCursorLL.lng]]);
        }
    }

    function onClick(e: L.LeafletMouseEvent): void {
        if (!tool || e.originalEvent.detail > 1) return; // skip 2nd click of dblclick
        if (Date.now() < suppressClickUntil) return; // swallow click that follows a drag-commit
        const ll = e.latlng;

        if (tool === "text") {
            commit("text", [[ll.lat, ll.lng]], { label: getLabel() });
            return;
        }

        if (tool === "line" || tool === "arrow") {
            if (!state) {
                state = { points: [[ll.lat, ll.lng]] };
            } else {
                // Finish when clicking near the last-placed point - the natural
                // "I'm done" gesture, mirroring polygon's close-near-first-point below.
                const n = state.points.length;
                if (n >= 2) {
                    const lp = map.latLngToContainerPoint(L.latLng(state.points[n - 1]![0], state.points[n - 1]![1]));
                    const cp = map.latLngToContainerPoint(ll);
                    if (Math.hypot(lp.x - cp.x, lp.y - cp.y) <= 20) {
                        commit(tool, state.points.slice());
                        return;
                    }
                }
                state.points.push([ll.lat, ll.lng]);
            }
            preview(ll);
            hint();
            return;
        }

        if (tool === "polygon") {
            if (!state) {
                state = { points: [[ll.lat, ll.lng]] };
            } else {
                if (state.points.length >= 3) {
                    const fp = map.latLngToContainerPoint(L.latLng(state.points[0]![0], state.points[0]![1]));
                    const cp = map.latLngToContainerPoint(ll);
                    if (Math.hypot(fp.x - cp.x, fp.y - cp.y) <= 20) {
                        commit("polygon", state.points.slice());
                        return;
                    }
                }
                state.points.push([ll.lat, ll.lng]);
            }
            preview(ll);
            hint();
            return;
        }

        if (tool === "rect") {
            if (!state) {
                state = { points: [[ll.lat, ll.lng]] };
                hint();
            } else {
                commit("rect", [state.points[0]!, [ll.lat, ll.lng]]);
            }
            return;
        }

        if (tool === "circle") {
            if (!state) {
                state = { points: [[ll.lat, ll.lng]] };
                hint();
            } else {
                commit("circle", [state.points[0]!, [ll.lat, ll.lng]]);
            }
        }
    }

    function onDblClick(e: L.LeafletMouseEvent): void {
        if (!tool || !state) return;
        L.DomEvent.stop(e); // critical: prevent map zoom

        if (tool === "line" || tool === "arrow") {
            // The first click of the dblclick sequence already added a duplicate last point;
            // strip it unless doing so would leave us with fewer than 2 points.
            const pts = state.points.length > 2 ? state.points.slice(0, -1) : state.points.slice();
            if (pts.length >= 2) commit(tool, pts);
            return;
        }

        if (tool === "polygon") {
            const ppts = state.points.length > 3 ? state.points.slice(0, -1) : state.points.slice();
            if (ppts.length >= 3) commit("polygon", ppts);
        }
    }

    function onMouseMove(e: L.LeafletMouseEvent): void {
        lastCursorLL = e.latlng;
        if (state || tool === "rect" || tool === "circle") preview(e.latlng);
    }

    function onMouseDown(e: MouseEvent): void {
        if (!tool || e.button !== 0) return;
        const eligible = ["circle", "rect", "arrow", "line", "text"].includes(tool);
        if (!eligible) return;

        const startLL = map.mouseEventToLatLng(e);
        const startX = e.clientX;
        const startY = e.clientY;
        let isDragging = false;
        // For arrow/line: if we already have points placed via click, chain onto them
        const hasPoints = !!(state?.points.length && (tool === "arrow" || tool === "line"));

        function onMove(ev: MouseEvent): void {
            const dx = ev.clientX - startX;
            const dy = ev.clientY - startY;
            if (!isDragging && Math.hypot(dx, dy) < 6) return;
            isDragging = true;
            const endLL = map.mouseEventToLatLng(ev);
            const c = getColor();
            clearPrev();

            if (tool === "circle") {
                L.circle(startLL, { radius: startLL.distanceTo(endLL), color: c, weight: 2, fillOpacity: 0.1, interactive: false }).addTo(prevLayer);
            } else if (tool === "rect") {
                const rs = state && state.points.length >= 1 ? L.latLng(state.points[0]!) : startLL;
                L.rectangle(L.latLngBounds(rs, endLL), { color: c, weight: 2, fillOpacity: 0.08, dashArray: "4 4", interactive: false }).addTo(prevLayer);
            } else if (tool === "arrow" || tool === "line") {
                const pts: LatLngTuple[] = hasPoints ? [...state!.points, [endLL.lat, endLL.lng]] : [[startLL.lat, startLL.lng], [endLL.lat, endLL.lng]];
                L.polyline(pts, { color: c, weight: 2, opacity: 0.85, interactive: false }).addTo(prevLayer);
                if (tool === "arrow") {
                    const n = pts.length;
                    const deg = bearing(pts[n - 2]!, pts[n - 1]!);
                    const sz = 20;
                    L.marker(L.latLng(endLL.lat, endLL.lng), {
                        icon: L.divIcon({ className: "", html: arrowheadSvg(c, deg, sz), iconSize: [sz, sz], iconAnchor: [sz / 2, sz / 2] }),
                        interactive: false,
                    }).addTo(prevLayer);
                }
            } else if (tool === "text") {
                L.rectangle(L.latLngBounds(startLL, endLL), { color: c, weight: 1, dashArray: "3 4", fillOpacity: 0.04, interactive: false }).addTo(prevLayer);
            }
        }

        function onUp(ev: MouseEvent): void {
            document.removeEventListener("mousemove", onMove);
            clearPrev();
            const dx = ev.clientX - startX;
            const dy = ev.clientY - startY;
            if (!isDragging || Math.hypot(dx, dy) < 6) return; // too small - treat as click
            const endLL = map.mouseEventToLatLng(ev);
            // A completed drag still fires a native `click` right after this mouseup;
            // swallow it so it doesn't start a stray new point at the drag's end.
            suppressClickUntil = Date.now() + 350;

            if (tool === "circle") {
                commit("circle", [[startLL.lat, startLL.lng], [endLL.lat, endLL.lng]]);
            } else if (tool === "rect") {
                const rectStart: LatLngTuple = state && state.points.length >= 1 ? state.points[0]! : [startLL.lat, startLL.lng];
                commit("rect", [rectStart, [endLL.lat, endLL.lng]]);
            } else if (tool === "arrow" || tool === "line") {
                const finalPts: LatLngTuple[] = hasPoints ? [...state!.points, [endLL.lat, endLL.lng]] : [[startLL.lat, startLL.lng], [endLL.lat, endLL.lng]];
                commit(tool, finalPts);
            } else if (tool === "text") {
                // Drag defines an actual bounding box - both corners are committed
                // (mirroring rect) so the renderer can size/wrap the label to fit it,
                // instead of just deriving a font size from the drag distance.
                commit("text", [[startLL.lat, startLL.lng], [endLL.lat, endLL.lng]], { label: getLabel() });
            }
        }

        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp, { once: true });
    }

    function onKeyDown(e: KeyboardEvent): void {
        if (!tool) return;
        if (e.key === "Escape") {
            e.stopImmediatePropagation();
            e.preventDefault();
            if (state) cancelShape();
            else deactivate();
            return;
        }
        if (!state) return;
        if (e.key === "Enter") {
            e.stopImmediatePropagation();
            e.preventDefault();
            finishCurrent();
        }
    }

    map.on("click", onClick);
    map.on("dblclick", onDblClick);
    map.on("mousemove", onMouseMove);
    map.getContainer().addEventListener("mousedown", onMouseDown);
    document.addEventListener("keydown", onKeyDown, true);

    function destroy(): void {
        map.off("click", onClick);
        map.off("dblclick", onDblClick);
        map.off("mousemove", onMouseMove);
        map.getContainer().removeEventListener("mousedown", onMouseDown);
        document.removeEventListener("keydown", onKeyDown, true);
        if (map.hasLayer(prevLayer)) map.removeLayer(prevLayer);
    }

    return { startTool, deactivate, cancelShape, getCurrentTool, isBusy, canFinish, finishCurrent, destroy };
}

export const MarkupEngine = {
    bearing,
    arrowheadSvg,
    arrowheadSize,
    textLabelHtml,
    renderShape,
    createDrawSession,
};

export function installGlobalMarkupEngine(): void {
    window.MarkupEngine = MarkupEngine;
}

declare global {
    interface Window {
        MarkupEngine: typeof MarkupEngine;
    }
}
