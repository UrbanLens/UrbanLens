/**
 * Draws a {@link MarkupDocument} onto a Leaflet map so its shapes can be picked and changed: a click
 * or tap selects one, dragging it moves it, and handles reshape it - a point per vertex, a "+" on
 * each edge to add one, a "+" past each end of a line to extend it, a corner to scale and a stalk to
 * turn.
 *
 * Every measurement is in container pixels - where the shape is on screen - so moving, scaling and
 * turning mean what the viewer sees, including on a map that has itself been turned (leaflet-rotate
 * converts container points both ways). The document holds the result; this module only reads it
 * and reports edits back.
 */
import { MarkupEngine, type LatLngTuple, type ShapeSpec } from "./markup-engine";
import type { DocItem, MarkupDocument } from "./markup-document";
import {
    angleFrom,
    boxCenter,
    canRemoveVertex,
    canRotate,
    canScale,
    edgeMidpoints,
    editablePoints,
    extendPoints,
    hasEditableVertices,
    insertVertex,
    moveEditablePoint,
    removeVertex,
    rotateShape,
    scaleShape,
    translateShape,
    type Box,
    type Projection,
    type Pt,
} from "./markup-geometry";

// `L` is a page global from a CDN <script>; see markup-engine.ts.
declare const L: typeof import("leaflet");

/** Pane the handles live in, above every label and pin. */
const HANDLE_PANE = "ul-markup-handles";

/** How far a press has to travel before it is a drag rather than a click. A finger wobbles more than a mouse. */
const DRAG_THRESHOLD_MOUSE = 3;
const DRAG_THRESHOLD_TOUCH = 8;

/** Gaps between a shape's box and the handles drawn outside it. */
const EXTEND_OFFSET = 26;
const ROTATE_STALK = 30;
const SCALE_OFFSET = 10;
/** Space between a path and the box drawn around it when picked. */
const OUTLINE_PAD = 8;

export interface MarkupEditorOptions {
    /** Called after the map has redrawn for any change - the document's, or a picked point's. */
    onChange?: () => void;
    /** Shown while the viewer does something the shape cannot take, such as removing a line's last point. */
    onNotice?: (text: string) => void;
}

export interface MarkupEditor {
    /** Whether shapes take clicks. Off while a draw tool is armed, so a click draws rather than picks. */
    setInteractive: (on: boolean) => void;
    /** Redraws everything - after the map turns, say, when every arrowhead has to follow its line. */
    redraw: () => void;
    /** The picked vertex of the selected shape, if one is. */
    activeVertex: () => number | null;
    /** Whether the picked vertex can go without leaving the shape too few to draw. */
    canRemoveActiveVertex: () => boolean;
    removeActiveVertex: () => boolean;
    /** Removes the picked vertex if there is one, else the selected shape. */
    deleteSelection: () => boolean;
    /** Lets go of the picked vertex, then of the selection. @returns Whether there was anything to let go of. */
    escape: () => boolean;
    /** Moves the selected shape by screen pixels. */
    nudge: (dx: number, dy: number) => boolean;
    /** Whether a handle or the shape itself is being dragged right now. */
    isDragging: () => boolean;
    destroy: () => void;
}

interface Rendered {
    layers: L.Layer[];
}

type DragKind =
    | { kind: "body" }
    | { kind: "vertex"; index: number; from: Pt }
    | { kind: "insert"; index: number; from: Pt }
    | { kind: "scale"; origin: Pt }
    | { kind: "rotate"; origin: Pt };

interface Drag {
    /** What a press that never became a drag does instead. */
    onTap?: () => void;
    pointerId: number;
    id: string;
    what: DragKind;
    original: ShapeSpec;
    start: Pt;
    latest: Pt;
    moved: boolean;
    threshold: number;
    frame: number | null;
    shiftKey: boolean;
}

export function createMarkupEditor(map: L.Map, doc: MarkupDocument, options: MarkupEditorOptions = {}): MarkupEditor {
    const itemsGroup = L.layerGroup().addTo(map);
    const selectionGroup = L.layerGroup().addTo(map);
    const rendered = new Map<string, Rendered>();
    let interactive = true;
    let activeVertexIndex: number | null = null;
    let drag: Drag | null = null;
    let suppressClickUntil = 0;

    /**
     * Whether this click is the one a drag ended with, which is the drag's and not a new pick. Only the
     * first click inside the window is: a later one - picking another shape straight away, or the map
     * reopened after a quick save - is the viewer's.
     */
    function consumeDragClick(): boolean {
        if (Date.now() >= suppressClickUntil) return false;
        suppressClickUntil = 0;
        return true;
    }
    let destroyed = false;

    if (!map.getPane(HANDLE_PANE)) {
        // Beside the marker pane rather than in the map pane: leaflet-rotate keeps markers upright in a
        // pane of its own, and handles must sit in the same frame as the labels they are drawn around.
        const parent = map.getPane("markerPane")?.parentElement ?? undefined;
        map.createPane(HANDLE_PANE, parent).style.zIndex = "640";
    }

    /**
     * Container pixels, unrounded. Leaflet's own `latLngToContainerPoint` rounds to whole pixels, so
     * every point an edit passed through it would land up to half a pixel off - a drag straight
     * sideways would move a shape up or down too. Built from the map's centre instead: the view is
     * its projection about the centre, turned by however far leaflet-rotate has turned it.
     */
    function viewTransform(): { zoom: number; center: L.Point; half: L.Point; cos: number; sin: number } {
        const zoom = map.getZoom();
        const turned = map as L.Map & { getBearing?: () => number; options: { rotate?: boolean } };
        const radians = turned.options.rotate && typeof turned.getBearing === "function" ? (turned.getBearing() * Math.PI) / 180 : 0;
        return { zoom, center: map.project(map.getCenter(), zoom), half: map.getSize().divideBy(2), cos: Math.cos(radians), sin: Math.sin(radians) };
    }

    const proj: Projection = {
        project: (ll) => {
            const t = viewTransform();
            const p = map.project(L.latLng(ll[0], ll[1]), t.zoom);
            const dx = p.x - t.center.x;
            const dy = p.y - t.center.y;
            return { x: t.half.x + dx * t.cos - dy * t.sin, y: t.half.y + dx * t.sin + dy * t.cos };
        },
        unproject: (point) => {
            const t = viewTransform();
            const dx = point.x - t.half.x;
            const dy = point.y - t.half.y;
            const ll = map.unproject(L.point(t.center.x + dx * t.cos + dy * t.sin, t.center.y - dx * t.sin + dy * t.cos), t.zoom);
            return [ll.lat, ll.lng];
        },
    };

    /** The on-screen angle of a segment, so an arrowhead points along its line however the map is turned. */
    function screenAngle(from: LatLngTuple, to: LatLngTuple): number {
        return angleFrom(proj.project(from), proj.project(to));
    }

    // -- Drawing the shapes ---------------------------------------------------------------------

    function tag(layer: L.Layer, id: string, selected: boolean): void {
        const el = (layer as L.Path | L.Marker).getElement?.();
        if (!el) return;
        el.setAttribute("data-markup-id", id);
        el.classList.toggle("markup-editor-item", interactive);
        el.classList.toggle("markup-editor-item--selected", selected);
    }

    function renderItem(item: DocItem): void {
        const group = L.layerGroup().addTo(itemsGroup);
        const layers = MarkupEngine.renderShape(item.shape, group, undefined, { interactive, screenAngle });
        const selected = doc.selectedId() === item.id;
        for (const layer of layers) {
            tag(layer, item.id, selected);
            if (!interactive) continue;
            layer.on("click", (event: L.LeafletMouseEvent) => {
                L.DomEvent.stop(event as unknown as Event);
                if (consumeDragClick()) return;
                if (doc.selectedId() !== item.id) activeVertexIndex = null;
                doc.select(item.id);
            });
            // A long press on a phone is a context menu. Picking the shape is what it should do here.
            layer.on("contextmenu", (event: L.LeafletMouseEvent) => {
                L.DomEvent.stop(event as unknown as Event);
                event.originalEvent?.preventDefault();
                doc.select(item.id);
            });
        }
        rendered.set(item.id, { layers });
    }

    function renderItems(): void {
        itemsGroup.clearLayers();
        rendered.clear();
        for (const item of doc.items()) {
            if (!item.hidden) renderItem(item);
        }
    }

    // -- The selection --------------------------------------------------------------------------

    /** The selected label's or pin's drawn size, which only the page knows. */
    function markerSize(id: string): { width: number; height: number } | undefined {
        for (const layer of rendered.get(id)?.layers ?? []) {
            const el = (layer as L.Marker).getElement?.();
            const body = el?.firstElementChild as HTMLElement | null | undefined;
            if (body && body.offsetWidth) return { width: body.offsetWidth, height: body.offsetHeight };
        }
        return undefined;
    }

    /** A label's corners on screen, turned as it is drawn. */
    function labelCorners(shape: ShapeSpec, size: { width: number; height: number }): Pt[] {
        const anchor = proj.project(shape.latlngs[0]!);
        const center = { x: anchor.x + size.width / 2, y: anchor.y + size.height / 2 };
        const radians = (MarkupEngine.textRotation(shape) * Math.PI) / 180;
        const cos = Math.cos(radians);
        const sin = Math.sin(radians);
        return [
            [-1, -1],
            [1, -1],
            [1, 1],
            [-1, 1],
        ].map(([sx, sy]) => {
            const x = (sx! * size.width) / 2;
            const y = (sy! * size.height) / 2;
            return { x: center.x + x * cos - y * sin, y: center.y + x * sin + y * cos };
        });
    }

    /** The selected shape's outline on screen: its corners, and the square box around them. */
    function outline(item: DocItem): { corners: Pt[]; box: Box } {
        const shape = item.shape;
        let corners: Pt[];
        if (shape.type === "text") {
            corners = labelCorners(shape, markerSize(item.id) ?? { width: 40, height: 20 });
        } else if (shape.type === "pin") {
            const size = markerSize(item.id) ?? { width: 32, height: 32 };
            const anchor = proj.project(shape.latlngs[0]!);
            corners = [
                { x: anchor.x - size.width / 2, y: anchor.y - size.height },
                { x: anchor.x + size.width / 2, y: anchor.y - size.height },
                { x: anchor.x + size.width / 2, y: anchor.y },
                { x: anchor.x - size.width / 2, y: anchor.y },
            ];
        } else if (shape.type === "circle") {
            const c = proj.project(shape.latlngs[0]!);
            const e = proj.project(shape.latlngs[1]!);
            const r = Math.hypot(e.x - c.x, e.y - c.y);
            corners = [
                { x: c.x - r, y: c.y - r },
                { x: c.x + r, y: c.y - r },
                { x: c.x + r, y: c.y + r },
                { x: c.x - r, y: c.y + r },
            ];
        } else {
            corners = editablePoints(shape).map((ll) => proj.project(ll));
        }
        const xs = corners.map((p) => p.x);
        const ys = corners.map((p) => p.y);
        if (shape.type === "text" || shape.type === "pin" || shape.type === "circle") {
            return { corners, box: { minX: Math.min(...xs), minY: Math.min(...ys), maxX: Math.max(...xs), maxY: Math.max(...ys) } };
        }
        // Clear of the line itself, so a straight line still gets a box around it rather than a line on it.
        const pad = Math.max(OUTLINE_PAD, (shape.stroke_width ?? 3) / 2 + 4);
        const box = { minX: Math.min(...xs) - pad, minY: Math.min(...ys) - pad, maxX: Math.max(...xs) + pad, maxY: Math.max(...ys) + pad };
        return { corners: [
            { x: box.minX, y: box.minY },
            { x: box.maxX, y: box.minY },
            { x: box.maxX, y: box.maxY },
            { x: box.minX, y: box.maxY },
        ], box };
    }

    function iconElement(name: string): HTMLElement {
        const i = document.createElement("i");
        i.className = "material-symbols-outlined";
        i.setAttribute("aria-hidden", "true");
        i.textContent = name;
        return i;
    }

    /** A handle: a press drags it, and a press that does not move is a tap. */
    function handle(at: Pt, className: string, label: string, icon: string | null, onDown: (event: PointerEvent) => void): L.Marker {
        const marker = L.marker(L.latLng(...proj.unproject(at)), {
            pane: HANDLE_PANE,
            keyboard: false,
            interactive: true,
            bubblingMouseEvents: false,
            icon: L.divIcon({
                className: `markup-handle ${className}`,
                html: "",
                iconSize: undefined,
                iconAnchor: undefined,
            }),
        }).addTo(selectionGroup);
        const el = marker.getElement();
        if (el) {
            if (icon) el.appendChild(iconElement(icon));
            el.setAttribute("role", "button");
            el.setAttribute("aria-label", label);
            el.title = label;
            // Neither the map's own drag nor a map click may see a press on a handle.
            L.DomEvent.disableClickPropagation(el);
            el.addEventListener("pointerdown", (event) => {
                if (event.button !== 0 || !event.isPrimary) return;
                event.stopPropagation();
                event.preventDefault();
                onDown(event);
            });
            // Taps are read off the press itself (see onDragEnd): a handle is drawn afresh as the
            // selection changes, and a click can land on one that has already gone.
            el.addEventListener("click", (event) => event.stopPropagation());
        }
        return marker;
    }

    function renderSelection(): void {
        selectionGroup.clearLayers();
        const item = doc.get(doc.selectedId());
        if (!item || item.hidden || !interactive) {
            activeVertexIndex = null;
            return;
        }
        const shape = item.shape;
        if (activeVertexIndex !== null && (!hasEditableVertices(shape) || activeVertexIndex >= shape.latlngs.length)) activeVertexIndex = null;
        const { corners, box } = outline(item);
        L.polygon(corners.map((p) => proj.unproject(p)), { className: "markup-selection-outline", interactive: false, fill: false, weight: 1.5, dashArray: "5 4" }).addTo(selectionGroup);

        // Reshape: every point, plus a "+" in each edge and past each end of a line.
        if (shape.type !== "text" && shape.type !== "pin") {
            editablePoints(shape).forEach((ll, index) => {
                const at = proj.project(ll);
                const active = index === activeVertexIndex;
                const name = shape.type === "circle" ? (index === 0 ? "Move circle" : "Resize circle") : shape.type === "rect" ? "Resize from this corner" : `Point ${index + 1}`;
                handle(at, `markup-handle--vertex${active ? " is-active" : ""}`, hasEditableVertices(shape) ? `${name} - drag to move, tap to pick, double-tap to remove` : name, null, (event) =>
                    startDrag(event, item, { kind: "vertex", index, from: at }, () => tapVertex(index)),
                );
            });
            for (const mid of edgeMidpoints(shape, proj)) {
                const at = proj.project(mid.ll);
                handle(at, "markup-handle--insert", "Add a point here", "add", (event) => startDrag(event, item, { kind: "insert", index: mid.index, from: at }, () => insertAt(item.id, mid.index, at)));
            }
            const ends = extendPoints(shape, proj, EXTEND_OFFSET);
            if (ends) {
                const startAt = proj.project(ends.start);
                const endAt = proj.project(ends.end);
                const last = shape.latlngs.length;
                handle(startAt, "markup-handle--extend", "Extend from the start", "add", (event) => startDrag(event, item, { kind: "insert", index: 0, from: startAt }, () => insertAt(item.id, 0, startAt)));
                handle(endAt, "markup-handle--extend", "Extend from the end", "add", (event) => startDrag(event, item, { kind: "insert", index: last, from: endAt }, () => insertAt(item.id, last, endAt)));
            }
        }

        const center = boxCenter(box);
        if (canScale(shape) && shape.type !== "circle") {
            const at = { x: box.maxX + SCALE_OFFSET, y: box.maxY + SCALE_OFFSET };
            handle(at, "markup-handle--scale", "Drag to make bigger or smaller", "open_in_full", (event) => startDrag(event, item, { kind: "scale", origin: shape.type === "text" ? corners[0]! : center }));
        }
        if (canRotate(shape)) {
            const top = { x: center.x, y: box.minY };
            const at = { x: center.x, y: box.minY - ROTATE_STALK };
            L.polyline([proj.unproject(top), proj.unproject(at)], { className: "markup-selection-outline", interactive: false, weight: 1.5 }).addTo(selectionGroup);
            handle(at, "markup-handle--rotate", "Drag to turn (Shift snaps to 15°)", "rotate_right", (event) => startDrag(event, item, { kind: "rotate", origin: center }));
        }
    }

    /** Two taps on the same point inside this long remove it, like a double-click. */
    const DOUBLE_TAP_MS = 400;
    let lastTap: { index: number; at: number } | null = null;

    function tapVertex(index: number): void {
        const now = Date.now();
        const repeat = lastTap && lastTap.index === index && now - lastTap.at < DOUBLE_TAP_MS;
        lastTap = repeat ? null : { index, at: now };
        const item = doc.get(doc.selectedId());
        if (!item || !hasEditableVertices(item.shape)) return;
        if (repeat) {
            activeVertexIndex = index;
            removeActiveVertex();
            return;
        }
        activeVertexIndex = activeVertexIndex === index ? null : index;
        renderSelection();
        options.onChange?.();
    }

    function insertAt(id: string, index: number, at: Pt): void {
        if (doc.update(id, (shape) => insertVertex(shape, index, proj.unproject(at)))) {
            activeVertexIndex = index;
            renderSelection();
        }
    }

    // -- Dragging -------------------------------------------------------------------------------

    function containerPoint(event: PointerEvent | MouseEvent): Pt {
        const p = map.mouseEventToContainerPoint(event as MouseEvent);
        return { x: p.x, y: p.y };
    }

    function applyDrag(state: Drag): void {
        const current = state.latest;
        const dx = current.x - state.start.x;
        const dy = current.y - state.start.y;
        const what = state.what;
        doc.update(state.id, () => {
            switch (what.kind) {
                case "body":
                    return translateShape(state.original, dx, dy, proj);
                case "vertex":
                    return moveEditablePoint(state.original, what.index, proj.unproject({ x: what.from.x + dx, y: what.from.y + dy }), proj);
                case "insert":
                    return insertVertex(state.original, what.index, proj.unproject({ x: what.from.x + dx, y: what.from.y + dy }));
                case "scale": {
                    const before = Math.hypot(state.start.x - what.origin.x, state.start.y - what.origin.y) || 1;
                    const after = Math.hypot(current.x - what.origin.x, current.y - what.origin.y);
                    return scaleShape(state.original, Math.max(0.05, Math.min(20, after / before)), what.origin, proj);
                }
                case "rotate": {
                    let degrees = angleFrom(what.origin, current) - angleFrom(what.origin, state.start);
                    if (state.shiftKey) degrees = Math.round(degrees / 15) * 15;
                    return rotateShape(state.original, degrees, what.origin, proj);
                }
            }
            return undefined;
        });
    }

    function startDrag(event: PointerEvent, item: DocItem, what: DragKind, onTap?: () => void): void {
        if (drag || !interactive) return;
        const start = containerPoint(event);
        drag = {
            onTap,
            pointerId: event.pointerId,
            id: item.id,
            what,
            original: item.shape,
            start,
            latest: start,
            moved: false,
            threshold: event.pointerType === "touch" ? DRAG_THRESHOLD_TOUCH : DRAG_THRESHOLD_MOUSE,
            frame: null,
            shiftKey: event.shiftKey,
        };
        doc.begin();
        // A finger dragging a handle must not also pan the map or scroll the page.
        map.dragging.disable();
        map.getContainer().style.touchAction = "none";
        window.addEventListener("pointermove", onDragMove);
        window.addEventListener("pointerup", onDragEnd);
        window.addEventListener("pointercancel", onDragCancel);
    }

    function onDragMove(event: PointerEvent): void {
        const state = drag;
        if (!state) return;
        // A second finger means a pinch, not a reshape: put the shape back and let the map have it.
        if (event.pointerId !== state.pointerId) {
            if (event.type === "pointermove" && !event.isPrimary) onDragCancel(event, true);
            return;
        }
        const point = containerPoint(event);
        state.shiftKey = event.shiftKey;
        if (!state.moved && Math.hypot(point.x - state.start.x, point.y - state.start.y) < state.threshold) return;
        state.moved = true;
        state.latest = point;
        if (state.frame === null) {
            state.frame = window.requestAnimationFrame(() => {
                state.frame = null;
                if (drag === state) applyDrag(state);
            });
        }
    }

    function endDrag(): Drag | null {
        const state = drag;
        drag = null;
        window.removeEventListener("pointermove", onDragMove);
        window.removeEventListener("pointerup", onDragEnd);
        window.removeEventListener("pointercancel", onDragCancel);
        if (state?.frame != null) window.cancelAnimationFrame(state.frame);
        if (!destroyed) {
            map.dragging.enable();
            map.getContainer().style.touchAction = "";
        }
        return state;
    }

    function onDragEnd(event: PointerEvent): void {
        if (!drag || event.pointerId !== drag.pointerId) return;
        const state = endDrag()!;
        if (!state.moved) {
            doc.commit();
            state.onTap?.();
            return;
        }
        state.latest = containerPoint(event);
        applyDrag(state);
        // The click that follows a drag is the drag's, not a new pick.
        suppressClickUntil = Date.now() + 350;
        const what = state.what;
        const shape = doc.get(state.id)?.shape;
        if (what.kind === "insert" || (what.kind === "vertex" && shape && hasEditableVertices(shape))) activeVertexIndex = what.index;
        doc.commit();
        renderSelection();
        options.onChange?.();
    }

    function onDragCancel(event: PointerEvent, force = false): void {
        if (!drag || (!force && event.pointerId !== drag.pointerId)) return;
        endDrag();
        doc.cancel();
    }

    /** A press on the selected shape itself moves it - caught before the map's own drag sees it. */
    function onContainerPointerDown(event: PointerEvent): void {
        if (!interactive || drag || event.button !== 0) return;
        const target = event.target instanceof Element ? event.target.closest("[data-markup-id]") : null;
        const id = target?.getAttribute("data-markup-id");
        const item = doc.get(id ?? null);
        if (!item || id !== doc.selectedId()) return;
        if (!event.isPrimary) return;
        event.stopPropagation();
        startDrag(event, item, { kind: "body" });
    }

    /** Leaflet's map drag listens for these rather than pointer events, so they are stopped too. */
    function onContainerPress(event: MouseEvent | TouchEvent): void {
        if (!interactive) return;
        if ("touches" in event && event.touches.length > 1) return;
        const target = event.target instanceof Element ? event.target.closest("[data-markup-id]") : null;
        if (target && target.getAttribute("data-markup-id") === doc.selectedId()) event.stopPropagation();
    }

    const container = map.getContainer();
    container.addEventListener("pointerdown", onContainerPointerDown, true);
    container.addEventListener("mousedown", onContainerPress, true);
    container.addEventListener("touchstart", onContainerPress, { capture: true, passive: true });

    function onMapClick(): void {
        if (!interactive || consumeDragClick()) return;
        activeVertexIndex = null;
        doc.select(null);
    }
    map.on("click", onMapClick);

    // Handles are placed in screen pixels, so they move whenever the view does.
    function onViewChange(): void {
        if (!drag) renderSelection();
    }
    map.on("zoomend moveend", onViewChange);
    // Arrowheads are markers and stay upright, so on a turned map they are drawn again to follow their lines.
    function onRotate(): void {
        renderItems();
        renderSelection();
    }
    map.on("rotate", onRotate);

    const unsubscribe = doc.subscribe((change) => {
        if (change.kind !== "selection") renderItems();
        else {
            // Only the selection moved: the shapes stay, their selected class does not.
            for (const [id, entry] of rendered) for (const layer of entry.layers) tag(layer, id, id === doc.selectedId());
            activeVertexIndex = null;
        }
        if (!drag) renderSelection();
        options.onChange?.();
    });

    function removeActiveVertex(): boolean {
        const id = doc.selectedId();
        const item = doc.get(id);
        if (!item || activeVertexIndex === null) return false;
        const next = removeVertex(item.shape, activeVertexIndex);
        if (!next) {
            options.onNotice?.(item.shape.type === "polygon" ? "A polygon needs at least 3 points." : "A line needs at least 2 points.");
            return false;
        }
        activeVertexIndex = null;
        return doc.update(item.id, () => next);
    }

    renderItems();
    renderSelection();

    return {
        setInteractive(on) {
            if (interactive === on) return;
            interactive = on;
            if (!on) {
                if (drag) onDragCancel(new PointerEvent("pointercancel"), true);
                activeVertexIndex = null;
                doc.select(null);
            }
            renderItems();
            renderSelection();
        },
        redraw() {
            renderItems();
            renderSelection();
        },
        activeVertex: () => activeVertexIndex,
        canRemoveActiveVertex() {
            const item = doc.get(doc.selectedId());
            return !!item && activeVertexIndex !== null && canRemoveVertex(item.shape);
        },
        removeActiveVertex,
        deleteSelection() {
            const id = doc.selectedId();
            if (!id) return false;
            if (activeVertexIndex !== null) return removeActiveVertex();
            return doc.remove(id);
        },
        escape() {
            if (drag) {
                onDragCancel(new PointerEvent("pointercancel"), true);
                return true;
            }
            if (activeVertexIndex !== null) {
                activeVertexIndex = null;
                renderSelection();
                options.onChange?.();
                return true;
            }
            if (doc.selectedId()) {
                doc.select(null);
                return true;
            }
            return false;
        },
        nudge(dx, dy) {
            const id = doc.selectedId();
            const item = doc.get(id);
            if (!item) return false;
            return doc.update(item.id, (shape) => translateShape(shape, dx, dy, proj));
        },
        isDragging: () => drag !== null,
        destroy() {
            if (drag) onDragCancel(new PointerEvent("pointercancel"), true);
            destroyed = true;
            unsubscribe();
            container.removeEventListener("pointerdown", onContainerPointerDown, true);
            container.removeEventListener("mousedown", onContainerPress, true);
            container.removeEventListener("touchstart", onContainerPress, { capture: true });
            map.off("click", onMapClick);
            map.off("zoomend moveend", onViewChange);
            map.off("rotate", onRotate);
            map.removeLayer(itemsGroup);
            map.removeLayer(selectionGroup);
        },
    };
}
