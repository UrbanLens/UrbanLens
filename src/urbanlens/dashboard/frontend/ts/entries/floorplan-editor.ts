/**
 * Floorplan editor: draw walls, get rooms.
 *
 * Walls are the only thing the user draws and the only geometry stored. Rooms
 * are *derived* - the enclosed regions of the wall graph - and a room's name
 * lives on a seed point that binds to whichever region contains it, so moving a
 * wall renames nothing and deletes nothing. Doors and windows are intervals
 * along a wall rather than objects in their own right, because they cannot
 * exist without one and must move with it.
 *
 * Geometry is authored in plan-local metres (see shared/floorplan/coords.ts)
 * and projected to WGS-84 only to hand coordinates to Leaflet.
 */

import { getCsrfToken } from "../shared/csrf";
import { toast } from "../shared/dialogs";
import { PlanProjection, type Pt, distance, interiorPoint, projectOnSegment, rotate } from "../shared/floorplan/coords";
import {
    type Floor,
    type Opening,
    type FloorplanDocument,
    type Marker,
    type MarkerKind,
    type RoomSeed,
    type VersionSummary,
    type Wall,
    emptyDocument,
    type ItemDetails,
    OPENING_KINDS,
    WALL_KINDS,
    attribute,
    copyFloorContents,
    nextLocalId,
    setAttribute,
    wallId,
    wallLength,
    wallSegments,
} from "../shared/floorplan/document";
import { contiguousLevels, deriveDesignations } from "../shared/floorplan/designations";
import { type DragModifiers, DragGesture, constrainToAxis, modifiersOf, snapRotation } from "../shared/floorplan/drag";
import { installGlobalIconPicker } from "../shared/icon-picker";
import { History } from "../shared/floorplan/history";
import { type Face, deriveFaces, faceForSeed } from "../shared/floorplan/planar";
import { PIXEL_TOLERANCES, clampOpening, snapPoint, snapTranslation } from "../shared/floorplan/snapping";
import { createMapImageOverlays, wireManageOverlaysDialog, type MapOverlayEntry } from "../shared/map-image-overlays";
import { createMapLayers } from "../shared/map-layers";

declare const L: typeof import("leaflet");

// @types/leaflet has no idea leaflet-rotate (loaded after leaflet.js in
// editor.html) exists - it patches L.Map in place rather than exporting its
// own types, so its additions are declared here rather than left as `any`.
declare module "leaflet" {
    interface MapOptions {
        rotate?: boolean;
        bearing?: number;
        touchRotate?: boolean;
        shiftKeyRotate?: boolean;
        rotateControl?: boolean | { position?: string; closeOnZeroBearing?: boolean };
    }
    interface Map {
        setBearing(theta: number): void;
        getBearing(): number;
    }
}

type Tool = "select" | "box" | "rotate" | "wall" | "opening" | "room" | "marker";

type SelectionItem =
    | { kind: "wall"; wall: Wall }
    | { kind: "room"; room: RoomSeed }
    | { kind: "marker"; marker: Marker }
    | { kind: "opening"; wall: Wall; opening: Opening };

/** Marker kinds that can join floors together. */
const CONNECTOR_KINDS = new Set<MarkerKind>(["stair", "elevator"]);

const WALL_STYLE: Record<string, { color: string; weight: number; dashArray?: string }> = {
    exterior: { color: "#263238", weight: 5 },
    interior: { color: "#546e7a", weight: 3 },
    // Finely dotted and warmer than the greys: a boundary, drawn as something
    // other than the building. Distinct from virtual's long dashes and
    // collapsed's gapped ones at a glance.
    fence: { color: "#8d6e63", weight: 2, dashArray: "1 4" },
    virtual: { color: "#90a4ae", weight: 2, dashArray: "6 6" },
    collapsed: { color: "#a1887f", weight: 3, dashArray: "2 6" },
};

const MARKER_ICON: Record<MarkerKind, string> = {
    hazard: "warning",
    stair: "stairs",
    elevator: "elevator",
};

/** Same palette family as the detail-pin map layer (map-annotations.ts's detailPinColors). */
const MARKER_COLOR: Record<MarkerKind, string> = {
    hazard: "#dc2626",
    stair: "#6b7280",
    elevator: "#6b7280",
};

// A white base once the exterior is drawn, so the plan reads as a real floor
// rather than a translucent overlay on the map beneath it. Rooms tint that
// base rather than replacing it, so named and unbound space both stay legible
// against a desaturated backdrop (see `.floorplan-map.has-plan` in the SCSS).
const ROOM_FILL = { color: "#00897b", weight: 1, fillColor: "#eef6f4", fillOpacity: 0.94 };
const UNBOUND_FILL = { color: "#b0bec5", weight: 1, dashArray: "4 4", fillColor: "#ffffff", fillOpacity: 0.92 };

/**
 * A marker icon, styled like the site's detail-pin markers elsewhere
 * (map-annotations.ts's `detailIcon()`): a colored glyph on a solid
 * background circle, for contrast against whatever is under it - a bare
 * glyph blended into the map beneath it.
 */
// A marker's icon/color prefer its linked detail pin's own customizations
// (set via the pin detail page's detail-pin dialog - see Marker.icon/color)
// over the kind-based defaults, the same priority detailIcon() in
// map-annotations.ts gives a plain detail pin.
function markerIcon(marker: Marker, selected: boolean): L.DivIcon {
    const color = marker.color || MARKER_COLOR[marker.kind] || "#2563eb";
    const glyph = marker.icon || MARKER_ICON[marker.kind] || "place";
    const size = 22;
    const pad = 5;
    const total = size + pad * 2;
    const ring = selected ? "outline:3px solid #00838f;outline-offset:2px;" : "";
    return L.divIcon({
        className: "floorplan-marker",
        html: `<span style="background:#fff;border:2px solid ${color};${ring}" class="floorplan-marker__badge"><span class="material-symbols-outlined" style="color:${color};font-size:${size}px;">${glyph}</span></span>`,
        iconSize: [total, total],
        iconAnchor: [total / 2, total],
        popupAnchor: [0, -total],
    });
}

function markerPopupContent(marker: Marker): HTMLElement {
    const wrap = document.createElement("div");
    wrap.className = "floorplan-marker-popup";
    const title = document.createElement("strong");
    title.textContent = marker.name || marker.kind;
    wrap.appendChild(title);
    // Only when the name says something the kind doesn't already - a marker
    // left at its default ("Elevator") has nothing more to add by repeating
    // "elevator" underneath it.
    if (marker.name && marker.name.trim().toLowerCase() !== marker.kind.toLowerCase()) {
        const kind = document.createElement("p");
        kind.className = "floorplan-marker-popup__kind";
        kind.textContent = marker.kind;
        wrap.appendChild(kind);
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn btn--sm btn--danger floorplan-marker-popup__delete";
    remove.textContent = "Delete";
    wrap.appendChild(remove);
    return wrap;
}

function boot(): void {
    // The shared picker's markup calls IconPicker.* from inline onclick, so the
    // global has to exist before any of it is clicked.
    installGlobalIconPicker();
    const mapElement = document.getElementById("floorplan-map");
    if (!mapElement) return;
    // Rebound so the null check survives into the closures below.
    const mapEl: HTMLElement = mapElement;

    const jsonUrl = mapEl.dataset.jsonUrl || "";
    const saveUrl = mapEl.dataset.saveUrl || "";
    const publishUrl = mapEl.dataset.publishUrl || "";
    const lat = parseFloat(mapEl.dataset.lat || "0");
    const lng = parseFloat(mapEl.dataset.lng || "0");

    // attributionControl: false - required attribution renders in the page
    // footer instead (show_map_footer=True; see createMapLayers' onAttribution below).
    // boxZoom: false - shift+drag is repurposed below for box-select instead
    // of Leaflet's default zoom-to-rectangle, which has no use here.
    // rotate/touchRotate/shiftKeyRotate/rotateControl (leaflet-rotate, loaded
    // in editor.html): lets a building that isn't square to true north be
    // turned to face the screen - two-finger twist on mobile, shift+wheel or
    // the rotate control's arrow on desktop. shiftKeyRotate is shift+*wheel*,
    // not shift+drag, so it does not collide with box-select above.
    const map = L.map("floorplan-map", {
        zoomControl: true,
        doubleClickZoom: false,
        attributionControl: false,
        boxZoom: false,
        rotate: true,
        touchRotate: true,
        shiftKeyRotate: true,
        rotateControl: { position: "topright", closeOnZeroBearing: false },
    }).setView([lat, lng], 20);

    // leaflet-rotate's own control is removed rather than re-homed. Reparenting
    // it into the toolbar made it *look* like one more tool while still
    // behaving like nothing else on the page: you had to press and drag on the
    // little arrow itself. Rotating is now a tool like the others - arm it, drag
    // anywhere, press Escape to leave - and the wheel gesture (shift+wheel,
    // untouched) stays as the shortcut for people who know it.
    map.getContainer().querySelector(".leaflet-control-rotate")?.remove();

    // Declared before createMapLayers below: its "underlay" custom toggle
    // reads state.showUnderlay synchronously while the panel builds its
    // initial button states, and a `const` referenced before its own
    // declaration line throws (temporal dead zone), not just "reads undefined".
    const state = {
        doc: emptyDocument({ lat, lng }),
        floorIndex: 0,
        tool: "select" as Tool,
        /** Chain of points for the wall currently being drawn. */
        drawing: [] as Pt[],
        cursor: null as Pt | null,
        snapKind: "" as string,
        /** The last-focused selected item - drives which edit form the sidebar shows. Always a member of `multi`. */
        selection: null as SelectionItem | null,
        /** Every currently selected item (ctrl+click or box-select can grow this past one). */
        multi: [] as SelectionItem[],
        markerKind: "hazard" as MarkerKind,
        /** What the wall tool draws next. */
        wallKind: "interior" as Wall["kind"],
        /** What the opening tool cuts next - this is where windows live. */
        openingKind: "door" as Opening["kind"],
        /** Snapping, as a setting. The backtick key suspends it momentarily. */
        snapEnabled: true,
        dirty: false,
        suspendSnap: false,
        faces: [] as Face[],
        versions: [] as VersionSummary[],
        showUnderlay: false,
        /** The plan could not be fetched, so what is on screen is not it. */
        loadFailed: false,
    };

    /**
     * The backdrop to trace over, via the same shared layers engine and panel
     * every other map on the site uses (satellite/street/topographic, plus
     * weather/borders overlays). Aerial by default: it shows the building's
     * footprint whichever storey is being drawn, which is exactly the part
     * that stays constant, so it remains the best available reference above
     * ground rather than a ground-floor-only aid. A georeferenced blueprint
     * overlay is better still where one exists (see "Image Overlays" in the
     * layers panel, wired to this pin's own overlay manager) and simply
     * renders on top - overlays live in Leaflet's overlayPane, not the
     * tilePane the desaturation below dims, so a traced blueprint stays crisp.
     */
    const mapLayers = createMapLayers(map, {
        root: document.getElementById("floorplan-layers"),
        defaultBase: "satellite",
        onAttribution: (text) => {
            const el = document.getElementById("page-footer-attribution-text");
            if (el) el.textContent = text;
        },
        custom: {
            underlay: {
                isActive: () => state.showUnderlay,
                toggle: () => {
                    state.showUnderlay = !state.showUnderlay;
                    render();
                },
            },
        },
    });

    const outline = readJson<Array<[number, number]>>("floorplan-outline") || [];
    // Always created, even with zero overlays at load time: the manage-overlays
    // dialog can add the pin's first one later, and without a live control to
    // sync() against, it would need a full page reload to actually appear.
    // Forced locked - overlays here are a traced-reference backdrop, and their
    // own drag handles would compete with the wall/marker tools' map gestures.
    const overlayControl = createMapImageOverlays(L, map, { cornersUrl: () => "", csrfToken: getCsrfToken() });
    const withForcedLock = (entries: MapOverlayEntry[]): MapOverlayEntry[] => entries.map((entry) => ({ ...entry, locked: true }));
    overlayControl.sync(withForcedLock(readJson<MapOverlayEntry[]>("floorplan-overlays") || []));
    document.body.addEventListener("ul:map-overlays-changed", (e) => {
        overlayControl.sync(withForcedLock((e as CustomEvent).detail?.overlays || []));
    });
    wireManageOverlaysDialog({
        map,
        control: overlayControl,
        onAlignStart: () => (document.getElementById("map-overlays-dialog") as HTMLDialogElement | null)?.close(),
    });

    let projection = new PlanProjection({ lat, lng });
    const wallLayer = L.layerGroup().addTo(map);
    const roomLayer = L.layerGroup().addTo(map);
    const markerLayer = L.layerGroup().addTo(map);
    // Rebuilt every render() - lets selectItem() find a marker's freshly
    // recreated Leaflet layer afterward (see its own comment for why).
    const markerNodes = new Map<Marker, L.Marker>();
    /** Room fills and their rings, so labels can be re-fitted after a zoom. */
    const roomLabels: Array<{ polygon: L.Polygon; ring: readonly Pt[] }> = [];
    const handleLayer = L.layerGroup().addTo(map);
    const ghostLayer = L.layerGroup().addTo(map);
    // Added first so it always paints beneath the live floor.
    const underlayLayer = L.layerGroup().addTo(map);

    const toLatLng = (p: Pt): [number, number] => {
        const world = projection.toWorld(p);
        return [world.lat, world.lng];
    };
    const toLocal = (latlng: L.LatLng): Pt => projection.toLocal({ lat: latlng.lat, lng: latlng.lng });

    /**
     * Whether snapping is off right now.
     *
     * Two controls, deliberately: a switch for someone tracing something that
     * genuinely is not square, and a held key for the one point in a drawing
     * that has to sit off the grid. Neither can express the other's case - a
     * toggle you must remember to turn back on is a trap, and a key you must
     * hold for ten minutes is not a setting.
     */
    const snapOff = (): boolean => state.suspendSnap || !state.snapEnabled;

    /** Snap tolerances in metres, derived from the fixed pixel tolerances. */
    function tolerances(): { endpoint: number; wall: number; extension: number } {
        const mpp = metresPerPixel();
        return {
            endpoint: PIXEL_TOLERANCES.endpoint * mpp,
            wall: PIXEL_TOLERANCES.wall * mpp,
            extension: PIXEL_TOLERANCES.extension * mpp,
        };
    }

    /**
     * How many drags are in flight.
     *
     * A drag re-renders the whole plan on every frame, and handles are pure
     * overhead while one is running: nothing can be grabbed that is not already
     * grabbed, and a joint handle per corner is a DOM node per corner rebuilt
     * per frame.
     */
    let activeDrags = 0;

    /** What a drag handler is told on every move. */
    interface DragFrame {
        /** Where the pointer is now, in plan-local metres. */
        local: Pt;
        /** Where the pointer is now, as a map container point. */
        pixel: L.Point;
        /** The modifiers held when the press landed, frozen for the gesture. */
        modifiers: DragModifiers;
    }

    /**
     * Attach a drag to a rendered layer.
     *
     * Bound in *pointer* events rather than mouse events, and on the layer's
     * own element rather than on the map. Both matter:
     *
     * - A finger drag emits no mouse events at all, so every drag in this
     *   editor except the Leaflet-native marker one was unreachable on a
     *   phone. Pointer events cover mouse, touch and pen through one path, so
     *   there is no second implementation to drift.
     * - Listeners on the element die with the element. Leaflet rebuilds these
     *   layers on every render, and render runs on every frame of every drag,
     *   so map-level listeners accumulated in their hundreds.
     *
     * ``setPointerCapture`` keeps the gesture attached to the thing that was
     * grabbed even when the pointer leaves it, which is what makes dragging a
     * 3px wall possible at all.
     *
     * Args:
     *     element: The layer's DOM node; nothing is bound when it is absent.
     *     handlers: ``start`` may return false to decline the gesture, leaving
     *         the press to the map (which is how pressing an unselected room
     *         still pans). ``move`` is called only once the pointer has
     *         travelled far enough to count as a drag. ``end`` is told whether
     *         it ever did.
     */
    function bindDrag(
        element: Element | undefined | null,
        handlers: {
            start?: (event: PointerEvent) => boolean | void;
            move: (frame: DragFrame) => void;
            end?: (moved: boolean) => void;
            slopPx?: number;
        },
    ): void {
        if (!element) return;
        element.addEventListener("pointerdown", (raw) => {
            const event = raw as PointerEvent;
            // Left button only for a mouse; any contact for touch or pen.
            if (event.pointerType === "mouse" && event.button !== 0) return;
            if (handlers.start?.(event) === false) return;
            // Stopped so Leaflet's own map-drag does not run this gesture too.
            // Propagation only, never preventDefault: suppressing the default
            // action here would also suppress the click that follows, and a
            // press that does not move is how everything gets selected.
            L.DomEvent.stopPropagation(event);
            // Disabled at the press rather than once the drag is live: on
            // touch, Leaflet starts panning from the same contact, and two
            // handlers would move the same finger's worth of distance.
            map.dragging.disable();
            activeDrags += 1;

            // The press identifies *what* was grabbed, but the rest of the
            // gesture is tracked on the map container, which outlives it.
            // render() runs on every frame of a drag and clears every layer,
            // so the element under the pointer is destroyed by the drag's own
            // first move - taking its listeners, and its pointer capture, with
            // it. Watching the element instead would end every drag after one
            // frame.
            const surface = map.getContainer();
            const gesture = new DragGesture({ x: event.clientX, y: event.clientY }, modifiersOf(event), handlers.slopPx);
            let moved = false;
            let finished = false;
            try {
                surface.setPointerCapture(event.pointerId);
            } catch {
                // Capture is an optimisation: it keeps a pointer that wanders
                // off the map reporting here. The listeners below still fire
                // without it.
            }

            const onMove = (rawMove: Event): void => {
                const moveEvent = rawMove as PointerEvent;
                if (moveEvent.pointerId !== event.pointerId) return;
                if (!gesture.advance({ x: moveEvent.clientX, y: moveEvent.clientY })) return;
                moved = true;
                const pixel = map.mouseEventToContainerPoint(moveEvent);
                handlers.move({ local: toLocal(map.containerPointToLatLng(pixel)), pixel, modifiers: gesture.modifiers });
            };
            const onFinish = (rawEnd: Event): void => {
                const endEvent = rawEnd as PointerEvent;
                if (endEvent.pointerId !== undefined && endEvent.pointerId !== event.pointerId) return;
                // pointerup releases capture, so lostpointercapture follows it -
                // without this the gesture would be ended twice.
                if (finished) return;
                finished = true;
                window.removeEventListener("pointermove", onMove);
                window.removeEventListener("pointerup", onFinish);
                window.removeEventListener("pointercancel", onFinish);
                window.removeEventListener("lostpointercapture", onFinish);
                activeDrags = Math.max(0, activeDrags - 1);
                map.dragging.enable();
                handlers.end?.(moved);
            };
            // On window rather than the map: a pointer released outside the map
            // still has to end the gesture, or the listeners stay and panning
            // stays disabled. Capture above normally retargets these to the
            // container anyway; this is what makes the failure case survivable.
            window.addEventListener("pointermove", onMove);
            window.addEventListener("pointerup", onFinish);
            window.addEventListener("pointercancel", onFinish);
            window.addEventListener("lostpointercapture", onFinish);
        });
    }

    /**
     * Snap a rigid translation against this floor, minus what the drag itself
     * rewrites.
     *
     * Args:
     *     moved: The carried points, at their pre-drag positions.
     *     delta: The translation asked for, in plan-local metres.
     *     exclude: Wall ids this drag mutates, which cannot be its own targets.
     */
    function snapDragTranslation(moved: readonly Pt[], delta: Pt, exclude: ReadonlySet<string>): Pt {
        if (snapOff()) return delta;
        const others = wallSegments(floor()).filter((segment) => !exclude.has(segment.wallId));
        return snapTranslation(moved, delta, others, tolerances());
    }

    /** Metres per screen pixel at the current zoom, for pixel-sized tolerances. */
    function metresPerPixel(): number {
        const centre = map.getCenter();
        const a = map.latLngToContainerPoint(centre);
        const b = L.point(a.x + 100, a.y);
        return map.distance(centre, map.containerPointToLatLng(b)) / 100;
    }

    const floor = (): Floor => {
        const floors = state.doc.floors;
        if (!floors.length) floors.push({ level: 0, name: "", walls: [], rooms: [], markers: [] });
        state.floorIndex = Math.min(state.floorIndex, floors.length - 1);
        return floors[state.floorIndex] as Floor;
    };

    function markDirty(): void {
        state.dirty = true;
        render();
        queueAutosave();
    }

    /** Like markDirty(), but skips the render - for a keystroke mid-edit
     * (a room/marker name field) where the caller already defers its own
     * re-render to a later event, so this would just be redundant work. */
    function markDirtyQuiet(): void {
        state.dirty = true;
        queueAutosave();
    }

    // ------------------------------------------------------------- autosave

    let autosaveTimer: ReturnType<typeof setTimeout> | null = null;
    let saving = false;
    /** The last save attempt failed and the document is still unsaved. */
    let saveFailed = false;
    /**
     * Backoff for retries, in milliseconds. A save fails for reasons that
     * usually clear on their own - a dropped connection, a restarting server -
     * so giving up after one attempt strands work the user cannot see is at
     * risk. The last delay repeats indefinitely rather than escalating.
     */
    const RETRY_DELAYS = [2000, 5000, 15000, 60000] as const;
    let retryAttempt = 0;

    /** Resolves once no save is in flight - callers that don't go through
     * queueAutosave() (Save as new version, Publish) still need this so their
     * request can never overlap one that is already running. */
    async function waitForSaveSlot(): Promise<void> {
        while (saving) await new Promise((resolve) => setTimeout(resolve, 100));
    }

    /** Debounced auto-save: there is no Save button any more (see editor.html) -
     * every edit is expected to reach the server on its own, a little after
     * the user stops making them. */
    function queueAutosave(delay = 1200): void {
        // The single funnel every edit reaches, so one guard here is enough:
        // what is on screen after a failed load is a blank document, not the
        // plan, and persisting it would replace the real one.
        if (state.loadFailed) return;
        updateSaveStatus();
        if (autosaveTimer !== null) clearTimeout(autosaveTimer);
        const attempt = (): void => {
            // A save from a moment ago is still in flight (slow network,
            // fast edits) - wait for it rather than firing a second
            // concurrent request that could land out of order.
            if (saving) {
                autosaveTimer = setTimeout(attempt, 400);
                return;
            }
            autosaveTimer = null;
            void save(false);
        };
        autosaveTimer = setTimeout(attempt, delay);
    }

    function updateSaveStatus(): void {
        const el = document.getElementById("floorplan-save-status");
        if (!el) return;
        const retry = document.getElementById("floorplan-retry-save");
        if (retry) retry.hidden = !saveFailed || saving;
        if (saving) {
            el.textContent = "Saving…";
            el.className = "floorplan-save-status";
        } else if (saveFailed) {
            el.textContent = "Not saved";
            el.className = "floorplan-save-status is-error";
        } else if (state.dirty) {
            el.textContent = "Unsaved changes";
            el.className = "floorplan-save-status is-unsaved";
        } else {
            el.textContent = "Saved";
            el.className = "floorplan-save-status";
        }
    }

    // ------------------------------------------------------------------ undo

    const cloneDocument = (doc: FloorplanDocument): FloorplanDocument => JSON.parse(JSON.stringify(doc)) as FloorplanDocument;
    const history = new History<FloorplanDocument>(cloneDocument);

    /**
     * Record the document as it stands, so the edit about to happen becomes one
     * undo step. Call before mutating, at the start of a gesture.
     *
     * Args:
     *     group: Collapses a run of related edits - successive keystrokes in
     *         one name field - into a single step.
     */
    function checkpoint(group: string | null = null): void {
        history.checkpoint(state.doc, group);
        updateHistoryButtons();
    }

    /** Forget both stacks - the document they describe is no longer loaded. */
    function clearHistory(): void {
        history.clear();
        updateHistoryButtons();
    }

    function updateHistoryButtons(): void {
        const undoButton = document.getElementById("floorplan-undo") as HTMLButtonElement | null;
        if (undoButton) undoButton.disabled = !history.canUndo;
        const redoButton = document.getElementById("floorplan-redo") as HTMLButtonElement | null;
        if (redoButton) redoButton.disabled = !history.canRedo;
    }

    /** Adopt a document restored from either direction of the history. */
    function applyHistoryState(doc: FloorplanDocument): void {
        state.doc = doc;
        clearSelection();
        // The restored floors array may be shorter than the one being viewed
        // (undoing a floor deletion's own inverse: adding one back works the
        // same way, via floorIndex clamping in floor() below).
        state.floorIndex = Math.min(state.floorIndex, Math.max(state.doc.floors.length - 1, 0));
        renderSidebar();
        render();
        updateHistoryButtons();
        markDirtyQuiet();
    }

    function undo(): void {
        const previous = history.undo(state.doc);
        if (previous === null) {
            toast.info("Nothing to undo.");
            return;
        }
        applyHistoryState(previous);
    }

    function redo(): void {
        const next = history.redo(state.doc);
        if (next === null) {
            toast.info("Nothing to redo.");
            return;
        }
        applyHistoryState(next);
    }

    document.getElementById("floorplan-retry-save")?.addEventListener("click", () => {
        // Straight to a save rather than re-arming the debounce: the user is
        // asking for it now, and waitForSaveSlot() inside save() already keeps
        // it from overlapping a retry that is mid-flight.
        retryAttempt = 0;
        void save(false);
    });
    document.getElementById("floorplan-undo")?.addEventListener("click", undo);
    document.getElementById("floorplan-redo")?.addEventListener("click", redo);
    updateHistoryButtons();

    // -------------------------------------------------------------- selection

    /** A stable identity for a selection item, independent of array position. */
    function itemKey(item: SelectionItem): string {
        if (item.kind === "opening") return `opening:${item.opening.uuid}`;
        if (item.kind === "wall") return `wall:${item.wall.uuid}`;
        if (item.kind === "room") return `room:${item.room.uuid}`;
        return `marker:${item.marker.uuid}`;
    }

    function isSelected(item: SelectionItem): boolean {
        const key = itemKey(item);
        return state.multi.some((existing) => itemKey(existing) === key);
    }

    function clearSelection(): void {
        state.selection = null;
        state.multi = [];
    }

    /**
     * Click-select one item, honoring ctrl/cmd for additive multi-select.
     *
     * A plain click replaces the selection with just this item; a ctrl/cmd
     * click toggles it in place, so building up (or trimming) a multi-select
     * one item at a time works the same way it does everywhere else.
     */
    function selectItem(item: SelectionItem, event: L.LeafletMouseEvent): void {
        const original = event.originalEvent as MouseEvent | undefined;
        const additive = Boolean(original && (original.ctrlKey || original.metaKey));
        if (additive) {
            const key = itemKey(item);
            const index = state.multi.findIndex((existing) => itemKey(existing) === key);
            if (index >= 0) state.multi.splice(index, 1);
            else state.multi.push(item);
            state.selection = state.multi.length ? (state.multi[state.multi.length - 1] as SelectionItem) : null;
        } else {
            state.multi = [item];
            state.selection = item;
        }
        renderSidebar();
        render();
        // A marker's own click handler and Leaflet's default bindPopup click
        // handling fire on the same native click that got us here - but
        // render() just tore down and rebuilt the whole marker layer, which
        // silently destroys whatever popup Leaflet opened in the meantime.
        // Reopen it on the freshly rebuilt node so it actually reaches the
        // screen.
        if (item.kind === "marker") markerNodes.get(item.marker)?.openPopup();
    }

    // ---------------------------------------------------------------- render

    function render(): void {
        wallLayer.clearLayers();
        roomLayer.clearLayers();
        markerLayer.clearLayers();
        handleLayer.clearLayers();

        const current = floor();
        // Drawing a frame must never edit the document. planar.ts bridges a
        // near-miss with a virtual edge so the region still closes while the
        // authored coordinates stay exactly as drawn; acting on that note by
        // welding the real endpoints destroyed geometry it had no mandate to
        // touch, and did it from inside the draw path where undo could not
        // see it. Seeds left orphaned by a deletion are pruned by whoever
        // did the deleting (see pruneOrphanedSeeds).
        const derived = deriveFaces(wallSegments(current));
        state.faces = derived.faces;

        // Once there's an exterior to read as "the building", the basemap
        // recedes (desaturated, in the tile pane only - an image overlay
        // renders in Leaflet's separate overlayPane, so a traced blueprint
        // stays crisp) and the plan itself becomes the thing in focus.
        mapEl.classList.toggle("has-plan", current.walls.some((wall) => wall.kind === "exterior"));

        // Each seed asked once which face is *its* room, rather than each face
        // asking every seed whether it is inside. Both give the same answer -
        // faceForSeed picks the smallest containing face, which is what stops a
        // hall wearing the name of a cupboard inside it - but asked the other
        // way round it is a face-squared scan, re-run on every frame of every
        // drag, since render() is what a drag calls.
        const seedForFace = new Map<Face, RoomSeed>();
        for (const room of current.rooms) {
            const bound = faceForSeed({ x: room.x, y: room.y }, derived.faces);
            if (bound && !seedForFace.has(bound)) seedForFace.set(bound, room);
        }

        // Rooms first so walls draw on top of their fills.
        roomLabels.length = 0;
        for (const face of derived.faces) {
            const seed = seedForFace.get(face);
            const roomSelected = seed ? isSelected({ kind: "room", room: seed }) : false;
            // A thicker border in a color one shade off the default teal read
            // as nearly the same room at a glance - a tinted fill on top of
            // it is what actually reads as "this one, selected" rather than
            // "this one, ever so slightly different."
            const polygon = L.polygon(face.ring.map(toLatLng), {
                className: "floorplan-room",
                ...(seed ? ROOM_FILL : UNBOUND_FILL),
                ...(roomSelected ? { color: "#00838f", weight: 4, fillColor: "#00838f", fillOpacity: 0.28 } : {}),
            }).addTo(roomLayer);
            const label = seed ? seed.name || "Unnamed" : "Unnamed room";
            // Permanent, not on hover: a room appearing and naming its own area
            // the instant a loop closes is what teaches the wall-first model,
            // and a badge nobody sees teaches nothing.
            // The area reads as secondary metadata, not part of the name
            // itself - a subtler line underneath rather than run in beside it.
            polygon.bindTooltip(`<span class="floorplan-room-label__name">${escHtml(label)}</span><span class="floorplan-room-label__area">${face.area.toFixed(1)} m²</span>`, {
                direction: "center",
                className: "floorplan-room-label",
                permanent: true,
            });
            roomLabels.push({ polygon, ring: face.ring });
            if (seed && roomSelected && state.multi.length === 1) renderRoomRotateGrip(seed, face);
            polygon.on("click", (event) => {
                // Checked before stopping propagation: a room fill covers a
                // large area, and a stop here regardless of tool silently
                // swallowed every wall/marker click landing inside a room -
                // exactly where someone is likeliest to want to add one.
                if (state.tool !== "select") return;
                L.DomEvent.stop(event);
                const bound = seed || addSeedAt(interiorPoint(face.ring));
                selectItem({ kind: "room", room: bound }, event);
            });
            polygon.on("contextmenu", (event) => {
                if (state.tool !== "select") return;
                const bound = seed || addSeedAt(interiorPoint(face.ring));
                showContextMenu(event, { kind: "room", room: bound });
            });
            // Dragging an already-selected room moves it as a whole: its own
            // unique walls translate rigidly together, and any wall it merely
            // borders (a shared partition, the exterior) stretches to follow
            // the corner it shares with this room while its own far end - not
            // part of this room at all - stays put. A plain click still only
            // selects first; this only engages on an actual drag of a room
            // that's already the selection, and never for a shift-drag (box-
            // select's own gesture, which starts from the same mousedown).
            let roomDrag: { local: Pt; boundary: NonNullable<ReturnType<typeof roomBoundaryWalls>>; origins: Map<Wall, { ax: number; ay: number; bx: number; by: number }>; ownPoints: Set<string>; seedOrigin: Pt } | null = null;
            bindDrag(polygon.getElement(), {
                start: (event) => {
                    if (state.tool !== "select" || !seed) return false;
                    if (event.shiftKey) return false;
                    // A plain press only selects; dragging a room you have not
                    // selected yet would move the building out from under a
                    // gesture that meant to pan.
                    if (!isSelected({ kind: "room", room: seed })) return false;
                    const boundary = roomBoundaryWalls(seed);
                    if (!boundary) return false;
                    // Every side is shell or shared, so a drag would translate
                    // nothing. Decline it, rather than taking the map's pan.
                    if (!boundary.unique.length) return false;
                    return true;
                },
                move: ({ local, modifiers }) => {
                    const bound = seed as RoomSeed;
                    if (!roomDrag) {
                        const boundary = roomBoundaryWalls(bound);
                        if (!boundary) return;
                        const key = (p: Pt): string => `${p.x},${p.y}`;
                        const ownPoints = new Set<string>();
                        for (const wall of boundary.unique) {
                            ownPoints.add(key({ x: wall.ax, y: wall.ay }));
                            ownPoints.add(key({ x: wall.bx, y: wall.by }));
                        }
                        const origins = new Map<Wall, { ax: number; ay: number; bx: number; by: number }>();
                        for (const wall of [...boundary.unique, ...boundary.shared]) origins.set(wall, { ax: wall.ax, ay: wall.ay, bx: wall.bx, by: wall.by });
                        roomDrag = { local, boundary, origins, ownPoints, seedOrigin: { x: bound.x, y: bound.y } };
                        checkpoint();
                    }
                    const { boundary, origins, ownPoints, seedOrigin } = roomDrag;
                    const key = (p: Pt): string => `${p.x},${p.y}`;
                    let dx = local.x - roomDrag.local.x;
                    let dy = local.y - roomDrag.local.y;
                    if (modifiers.constrain) {
                        const squared = constrainToAxis({ x: dx, y: dy }, (state.doc.rotation_degrees * Math.PI) / 180);
                        dx = squared.x;
                        dy = squared.y;
                    }
                    // Shared walls are stretched by this drag too, so they
                    // cannot be its snap targets - see the wall-body drag.
                    const carried = new Set([...boundary.unique, ...boundary.shared].map((item) => wallId(item)));
                    const corners: Pt[] = [];
                    for (const item of boundary.unique) {
                        const origin = origins.get(item) as { ax: number; ay: number; bx: number; by: number };
                        corners.push({ x: origin.ax, y: origin.ay }, { x: origin.bx, y: origin.by });
                    }
                    const snapped = snapDragTranslation(corners, { x: dx, y: dy }, carried);
                    dx = snapped.x;
                    dy = snapped.y;
                    for (const wall of boundary.unique) {
                        const orig = origins.get(wall) as { ax: number; ay: number; bx: number; by: number };
                        wall.ax = orig.ax + dx;
                        wall.ay = orig.ay + dy;
                        wall.bx = orig.bx + dx;
                        wall.by = orig.by + dy;
                    }
                    for (const wall of boundary.shared) {
                        const orig = origins.get(wall) as { ax: number; ay: number; bx: number; by: number };
                        if (ownPoints.has(key({ x: orig.ax, y: orig.ay }))) {
                            wall.ax = orig.ax + dx;
                            wall.ay = orig.ay + dy;
                        }
                        if (ownPoints.has(key({ x: orig.bx, y: orig.by }))) {
                            wall.bx = orig.bx + dx;
                            wall.by = orig.by + dy;
                        }
                    }
                    bound.x = seedOrigin.x + dx;
                    bound.y = seedOrigin.y + dy;
                    render();
                },
                end: (moved) => {
                    roomDrag = null;
                    if (moved) markDirty();
                },
            });
        }

        // Seeds that bind to no face - the "not enclosed" state.
        for (const room of current.rooms) {
            if (faceForSeed({ x: room.x, y: room.y }, derived.faces)) continue;
            L.circleMarker(toLatLng({ x: room.x, y: room.y }), { radius: 5, color: "#ef6c00", fillOpacity: 1 })
                .bindTooltip(`${escHtml(room.name || "Room")} — not enclosed`, { direction: "top" })
                .addTo(roomLayer);
        }

        for (const wall of current.walls) {
            // Captured on the first move rather than the press, so a press that
            // never becomes a drag costs nothing.
            let dragOrigin: { local: Pt; a: Pt; b: Pt } | null = null;
            let dragLinks: {
                stretchToA: Array<{ wall: Wall; end: "a" | "b" }>;
                stretchToB: Array<{ wall: Wall; end: "a" | "b" }>;
                network: Array<{ wall: Wall; end: "a" | "b"; origX: number; origY: number }>;
            } | null = null;
            const style = WALL_STYLE[wall.kind] || WALL_STYLE.interior;
            const selected = isSelected({ kind: "wall", wall });
            const a = { x: wall.ax, y: wall.ay };
            const b = { x: wall.bx, y: wall.by };
            const along = (t: number): Pt => ({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t });
            // A door/doorway/hatch renders as an actual break in the wall -
            // one solid segment per interval left once its openings are cut
            // out - rather than a colored line sitting on top of a wall that
            // reads as unbroken; a window stays an overlay on a continuous
            // wall, since it does not let anyone through (see renderOpenings).
            for (const [s0, s1] of wallSolidIntervals(wall)) {
                const line = L.polyline([toLatLng(along(s0)), toLatLng(along(s1))], {
                    className: "floorplan-wall",
                    ...style,
                    ...(selected ? { color: "#00838f", weight: (style?.weight || 3) + 2 } : {}),
                }).addTo(wallLayer);
                line.on("click", (event) => {
                    if (state.tool !== "select") return;
                    L.DomEvent.stop(event);
                    selectItem({ kind: "wall", wall }, event);
                });
                line.on("contextmenu", (event) => {
                    if (state.tool !== "select") return;
                    showContextMenu(event, { kind: "wall", wall });
                });
                // Dragging the wall's body moves the whole wall, not just one
                // endpoint (see renderWallHandles for that). Three modes, in
                // the editor's one modifier vocabulary:
                //   - default: this wall moves, and every other wall sharing
                //     one of its original endpoints stretches to follow that
                //     corner - its own other, unshared endpoint stays put.
                //   - Ctrl/Cmd ("take more"): the whole connected network -
                //     every wall reachable through a shared corner - moves
                //     together, rigidly.
                //   - Alt ("take less"): only this wall moves, detaching it;
                //     neighbours keep their original points entirely.
                // The mode is latched at the press and frozen for the gesture.
                // Read fresh on every move, as it used to be, one drag could
                // pass through all three and finish in a state matching none.
                bindDrag(line.getElement(), {
                    start: (event) => {
                        if (state.tool !== "select") return false;
                        if (event.shiftKey) return false; // box-select's own gesture
                        return true;
                    },
                    move: ({ local, modifiers }) => {
                        if (!dragOrigin) {
                            dragOrigin = { local, a: { x: wall.ax, y: wall.ay }, b: { x: wall.bx, y: wall.by } };
                            const current = floor();
                            dragLinks = {
                                stretchToA: wallsTouchingPoint(current, dragOrigin.a, wall),
                                stretchToB: wallsTouchingPoint(current, dragOrigin.b, wall),
                                network: connectedNetwork(current, wall),
                            };
                            checkpoint();
                        }
                        const links = dragLinks as NonNullable<typeof dragLinks>;
                        const origA = dragOrigin.a;
                        const origB = dragOrigin.b;
                        let dx = local.x - dragOrigin.local.x;
                        let dy = local.y - dragOrigin.local.y;
                        if (modifiers.constrain) {
                            const squared = constrainToAxis({ x: dx, y: dy }, (state.doc.rotation_degrees * Math.PI) / 180);
                            dx = squared.x;
                            dy = squared.y;
                        }
                        // Everything this drag rewrites, not just the wall under
                        // the cursor. A stretched neighbour's shared corner is
                        // moved to follow the wall on every frame, so leaving it
                        // in the candidate set means the wall is always within
                        // snapping distance of the endpoint it dragged there a
                        // frame ago - which pulls the delta back and freezes the
                        // drag after its first move.
                        const carried = modifiers.more
                            ? links.network.map((link) => link.wall)
                            : modifiers.less
                              ? [wall]
                              : [wall, ...links.stretchToA.map((link) => link.wall), ...links.stretchToB.map((link) => link.wall)];
                        const snapped = snapDragTranslation([origA, origB], { x: dx, y: dy }, new Set(carried.map((item) => wallId(item))));
                        dx = snapped.x;
                        dy = snapped.y;
                        if (modifiers.more) {
                            for (const link of links.network) {
                                if (link.end === "a") {
                                    link.wall.ax = link.origX + dx;
                                    link.wall.ay = link.origY + dy;
                                } else {
                                    link.wall.bx = link.origX + dx;
                                    link.wall.by = link.origY + dy;
                                }
                            }
                        } else {
                            wall.ax = origA.x + dx;
                            wall.ay = origA.y + dy;
                            wall.bx = origB.x + dx;
                            wall.by = origB.y + dy;
                            if (!modifiers.less) {
                                for (const link of links.stretchToA) {
                                    if (link.end === "a") {
                                        link.wall.ax = wall.ax;
                                        link.wall.ay = wall.ay;
                                    } else {
                                        link.wall.bx = wall.ax;
                                        link.wall.by = wall.ay;
                                    }
                                }
                                for (const link of links.stretchToB) {
                                    if (link.end === "a") {
                                        link.wall.ax = wall.bx;
                                        link.wall.ay = wall.by;
                                    } else {
                                        link.wall.bx = wall.bx;
                                        link.wall.by = wall.by;
                                    }
                                }
                            }
                        }
                        render();
                    },
                    end: (moved) => {
                        dragOrigin = null;
                        dragLinks = null;
                        if (moved) markDirty();
                    },
                });
            }
            renderOpenings(wall, selected);
        }

        // After the walls, so a joint sits on top of the lines it belongs to.
        // Not while dragging: they are one DOM node per corner, and whatever is
        // being dragged has already been grabbed.
        if (state.tool === "select" && !activeDrags) renderJointHandles(current);

        markerNodes.clear();
        for (const marker of current.markers) {
            const selected = isSelected({ kind: "marker", marker });
            const node = L.marker(toLatLng({ x: marker.x, y: marker.y }), { icon: markerIcon(marker, selected), draggable: state.tool === "select" }).addTo(markerLayer);
            markerNodes.set(marker, node);
            node.bindPopup(markerPopupContent(marker), { closeButton: true });
            node.on("popupopen", () => {
                node.getPopup()?.getElement()?.querySelector(".floorplan-marker-popup__delete")?.addEventListener("click", () => {
                    state.selection = { kind: "marker", marker };
                    state.multi = [state.selection];
                    deleteSelection();
                });
            });
            node.on("click", (event) => {
                if (state.tool !== "select") return;
                L.DomEvent.stop(event);
                selectItem({ kind: "marker", marker }, event);
            });
            node.on("contextmenu", (event) => {
                if (state.tool !== "select") return;
                showContextMenu(event, { kind: "marker", marker });
            });
            node.on("dragstart", () => checkpoint());
            node.on("dragend", () => {
                const p = toLocal(node.getLatLng());
                marker.x = p.x;
                marker.y = p.y;
                markDirty();
            });
        }

        renderUnderlay();
        renderFloorTabs();
        updateEmptyState(current);
        scheduleRoomLabelFit();
    }

    let labelFitFrame: number | null = null;

    /**
     * Re-fit the room labels once per frame at most.
     *
     * ``render()`` runs on every mousemove of every drag, and the fit pass
     * reads ``offsetWidth``, which forces a synchronous layout. Calling it
     * straight from render would put a reflow per room into every frame of
     * every drag.
     */
    function scheduleRoomLabelFit(): void {
        if (labelFitFrame !== null) return;
        labelFitFrame = requestAnimationFrame(() => {
            labelFitFrame = null;
            updateRoomLabelFit();
        });
    }

    /**
     * Hide a room's name once it no longer fits inside the room.
     *
     * A permanent centred label is what teaches the wall-first model - a room
     * naming itself the instant a loop closes - but zoomed out far enough the
     * name is wider than the room it belongs to, and a row of names sprawling
     * across each other's rooms obscures the very geometry they annotate.
     *
     * Hidden with ``visibility``, not ``display``: a label removed from layout
     * measures zero, would always "fit", and would flicker back on the next
     * pass.
     */
    function updateRoomLabelFit(): void {
        // Every measurement first, every write second. Toggling a class between
        // reads invalidates layout and forces a reflow per label.
        const decisions: Array<{ element: HTMLElement; fits: boolean }> = [];
        for (const { polygon, ring } of roomLabels) {
            const element = polygon.getTooltip()?.getElement();
            if (!element) continue;
            let minX = Infinity;
            let maxX = -Infinity;
            let minY = Infinity;
            let maxY = -Infinity;
            for (const point of ring) {
                const pixel = map.latLngToContainerPoint(toLatLng(point));
                minX = Math.min(minX, pixel.x);
                maxX = Math.max(maxX, pixel.x);
                minY = Math.min(minY, pixel.y);
                maxY = Math.max(maxY, pixel.y);
            }
            // The screen-space bounding box, which for a concave room is more
            // generous than the room itself - deliberately, since the cost of
            // hiding a name that would have fitted is higher than the cost of
            // keeping one that slightly overhangs.
            const fits = element.offsetWidth <= maxX - minX && element.offsetHeight <= maxY - minY;
            decisions.push({ element, fits });
        }
        for (const { element, fits } of decisions) element.classList.toggle("is-clipped", !fits);
    }

    /**
     * The parts of a wall's centreline still solid, once its door/doorway/
     * hatch openings are cut out of it.
     *
     * A window stays out of this: it reads as a break in the wall's own
     * *use* (you can walk through a door, not a window), so it renders as an
     * overlay on an otherwise-continuous wall instead - see renderOpenings().
     */
    /**
     * The wall nearest a point, within grabbing distance.
     *
     * Reports the distance as well as the wall, because callers deciding
     * whether to move something onto it need to know it is a real improvement
     * and not merely a different answer.
     *
     * Args:
     *     point: Where the pointer is, in plan-local metres.
     *
     * Returns:
     *     The nearest wall whose body is within the on-wall tolerance, with
     *     how far away it is, or null when the pointer is out in the open.
     */
    function wallNear(point: Pt): { wall: Wall; distance: number } | null {
        const tolerance = tolerances().wall;
        let best: { wall: Wall; distance: number } | null = null;
        for (const candidate of floor().walls) {
            const near = projectOnSegment(point, { x: candidate.ax, y: candidate.ay }, { x: candidate.bx, y: candidate.by });
            if (near.distance > tolerance) continue;
            if (!best || near.distance < best.distance) best = { wall: candidate, distance: near.distance };
        }
        return best;
    }

    /**
     * Move an opening onto a different wall, keeping the width it was given.
     *
     * An opening is stored as a fraction of the wall it sits in, so carrying
     * those two numbers across unchanged would make a door narrower on a long
     * wall and wider on a short one. The metre width is what the author
     * actually chose, so that is what survives the move.
     *
     * Args:
     *     opening: The opening to move.
     *     from: The wall it currently belongs to.
     *     to: The wall it should belong to.
     *     centreMeters: Where its middle should sit along the new wall.
     */
    function rehostOpening(opening: Opening, from: Wall, to: Wall, centreMeters: number): void {
        const widthMeters = (opening.t_end - opening.t_start) * wallLength(from);
        const length = wallLength(to);
        if (length < 1e-6) return;
        from.openings = from.openings.filter((item) => item !== opening);
        const halfWidth = Math.min(widthMeters, length) / 2;
        const centre = Math.min(Math.max(centreMeters, halfWidth), length - halfWidth);
        const [start, end] = clampOpening((centre - halfWidth) / length, (centre + halfWidth) / length);
        opening.t_start = start;
        opening.t_end = end;
        to.openings.push(opening);
    }

    function wallSolidIntervals(wall: Wall): Array<[number, number]> {
        const gaps = wall.openings
            // A window is the exception: you cannot walk through one, so it
            // draws over an unbroken wall rather than cutting it. A gate can
            // be walked through, so it reads as a real break like a door.
            .filter((opening) => opening.kind !== "window")
            .map((opening): [number, number] => [opening.t_start, opening.t_end])
            .sort((a, b) => a[0] - b[0]);
        const merged: Array<[number, number]> = [];
        for (const gap of gaps) {
            const last = merged[merged.length - 1];
            if (last && gap[0] <= last[1]) last[1] = Math.max(last[1], gap[1]);
            else merged.push(gap);
        }
        const solid: Array<[number, number]> = [];
        let cursor = 0;
        for (const gap of merged) {
            if (gap[0] > cursor) solid.push([cursor, gap[0]]);
            cursor = Math.max(cursor, gap[1]);
        }
        if (cursor < 1) solid.push([cursor, 1]);
        return solid;
    }

    function renderOpenings(wall: Wall, selected: boolean): void {
        const a = { x: wall.ax, y: wall.ay };
        const b = { x: wall.bx, y: wall.by };
        const at = (t: number): Pt => ({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t });
        for (const opening of wall.openings) {
            const openingSelected = isSelected({ kind: "opening", wall, opening });
            // A window stays a colored line over an otherwise-continuous
            // wall - it doesn't let anyone through, so a break would be the
            // wrong signal. A door/doorway/hatch already reads as a break in
            // the wall itself (see wallSolidIntervals()), so this only needs
            // to stay clickable there, not draw attention a second time -
            // opacity alone, not weight, so the actual hit/drag target stays
            // just as easy to grab as before.
            const isWindow = opening.kind === "window";
            const line = L.polyline([toLatLng(at(opening.t_start)), toLatLng(at(opening.t_end))], {
                className: "floorplan-opening",
                color: openingSelected ? "#00838f" : isWindow ? "#1e88e5" : "#fb8c00",
                weight: openingSelected ? 9 : 6,
                opacity: openingSelected || isWindow ? 1 : 0.45,
            })
                .bindTooltip(opening.kind, { direction: "top" })
                .addTo(wallLayer);
            // Selectable independently of its wall, so an opening can be
            // clicked and deleted (keyboard Delete, like every other
            // selectable item) without selecting the wall it sits in first.
            line.on("click", (event) => {
                if (state.tool !== "select") return;
                L.DomEvent.stop(event);
                selectItem({ kind: "opening", wall, opening }, event);
            });
            line.on("contextmenu", (event) => {
                if (state.tool !== "select") return;
                showContextMenu(event, { kind: "opening", wall, opening });
            });
            // Dragging the line itself slides the opening along its wall,
            // keeping its width fixed - on the same layer as the click
            // handler above rather than a separate overlay, so there is only
            // ever one target under the cursor to resolve a click against.
            // A plain click (no real movement) falls through to that click
            // handler exactly as before; this only acts once a real drag is
            // detected.
            let slide: { startT: number; width: number; originalStart: number; host: Wall } | null = null;
            bindDrag(line.getElement(), {
                start: () => state.tool === "select",
                move: ({ local, modifiers }) => {
                    if (!slide) {
                        slide = { startT: projectOnSegment(local, a, b).t, width: opening.t_end - opening.t_start, originalStart: opening.t_start, host: wall };
                        checkpoint();
                    }
                    // Dragged onto another wall, the opening goes with it - a
                    // door put on the wrong side of a room was otherwise a
                    // delete and a redraw. Alt ("take less") pins it to the wall
                    // it started on, for working into a corner where two walls
                    // are both within reach.
                    const host = slide.host;
                    const hostAway = projectOnSegment(local, { x: host.ax, y: host.ay }, { x: host.bx, y: host.by }).distance;
                    const nearest = modifiers.less ? null : wallNear(local);
                    // Strictly closer, not merely different. Near a corner both
                    // walls are within reach, so "the nearest wall that is not
                    // the one I am on" is a different answer every frame and the
                    // opening ping-pongs between them for as long as the pointer
                    // sits there.
                    const target = nearest && nearest.wall !== host && nearest.distance < hostAway ? nearest.wall : null;
                    if (target) {
                        const targetA = { x: target.ax, y: target.ay };
                        const targetB = { x: target.bx, y: target.by };
                        const along = projectOnSegment(local, targetA, targetB).t;
                        rehostOpening(opening, host, target, along * wallLength(target));
                        slide = { startT: along, width: opening.t_end - opening.t_start, originalStart: opening.t_start, host: target };
                        if (isSelected({ kind: "opening", wall: host, opening })) {
                            state.selection = { kind: "opening", wall: target, opening };
                            state.multi = [state.selection];
                        }
                        render();
                        return;
                    }
                    const hostA = { x: host.ax, y: host.ay };
                    const hostB = { x: host.bx, y: host.by };
                    const currentT = projectOnSegment(local, hostA, hostB).t;
                    const start = Math.max(0, Math.min(slide.originalStart + (currentT - slide.startT), 1 - slide.width));
                    opening.t_start = start;
                    opening.t_end = start + slide.width;
                    render();
                },
                end: (moved) => {
                    slide = null;
                    if (moved) markDirty();
                },
            });
            // The opening's own selection, not the wall's - selecting a door
            // replaces the whole selection with {kind: "opening", ...}, which
            // does not also count as its wall being selected, so gating this
            // on `selected` (the wall) meant these handles could only ever
            // appear if the wall happened to be selected too, which selecting
            // an opening directly never does.
            if (openingSelected) renderOpeningHandles(wall, opening, at);
        }
    }

    /**
     * Draggable ends for one opening, shown only while its wall is selected.
     *
     * An opening is an interval along its wall, so a handle drags in *t*
     * rather than in space: the cursor is projected back onto the wall and the
     * parameter clamped so the two ends cannot cross or leave the wall. That
     * is what keeps a door on its door frame when the wall is later moved.
     */
    function renderOpeningHandles(wall: Wall, opening: Opening, at: (t: number) => Pt): void {
        const MIN_WIDTH = 0.02; // as a fraction of the wall, so ends stay grabbable
        for (const end of ["t_start", "t_end"] as const) {
            const handle = L.circleMarker(toLatLng(at(opening[end])), {
                radius: 5,
                color: "#fb8c00",
                fillColor: "#fff",
                fillOpacity: 1,
                weight: 2,
                className: "floorplan-handle",
            }).addTo(handleLayer);
            let editing = false;
            bindDrag(handle.getElement(), {
                move: ({ local }) => {
                    if (!editing) {
                        editing = true;
                        checkpoint();
                    }
                    const along = projectOnSegment(local, { x: wall.ax, y: wall.ay }, { x: wall.bx, y: wall.by }).t;
                    if (end === "t_start") opening.t_start = Math.min(along, opening.t_end - MIN_WIDTH);
                    else opening.t_end = Math.max(along, opening.t_start + MIN_WIDTH);
                    opening.t_start = Math.max(0, opening.t_start);
                    opening.t_end = Math.min(1, opening.t_end);
                    render();
                },
                end: (moved) => {
                    editing = false;
                    if (moved) markDirty();
                },
            });
        }
    }

    /**
     * The floor below drawn faintly beneath the current one.
     *
     * Aligning a stairwell across storeys is otherwise guesswork, and this is
     * the cheapest way to make it possible: no new concepts, just the walls
     * you already drew, shown as context. Non-interactive so it can never be
     * selected or dragged by accident.
     */
    function renderUnderlay(): void {
        underlayLayer.clearLayers();
        const current = floor();
        const below = state.doc.floors.filter((item) => item.level < current.level).sort((x, y) => y.level - x.level)[0];
        // Offering "Floor below" when there is none to show is worse than no
        // toggle at all - it looks like the feature is broken rather than
        // inapplicable.
        document.querySelector<HTMLElement>('#floorplan-layers [data-map-layer="underlay"]')?.toggleAttribute("hidden", !below);
        if (!state.showUnderlay || !below) return;
        for (const wall of below.walls) {
            L.polyline([toLatLng({ x: wall.ax, y: wall.ay }), toLatLng({ x: wall.bx, y: wall.by })], {
                color: "#90a4ae",
                weight: 2,
                opacity: 0.45,
                interactive: false,
            }).addTo(underlayLayer);
        }
        for (const marker of below.markers) {
            if (!CONNECTOR_KINDS.has(marker.kind)) continue;
            L.circleMarker(toLatLng({ x: marker.x, y: marker.y }), {
                radius: 4,
                color: "#90a4ae",
                weight: 1,
                opacity: 0.6,
                fillOpacity: 0.2,
                interactive: false,
            }).addTo(underlayLayer);
        }
    }

    /**
     * A handle for turning a selected room.
     *
     * The room tool builds rectangles squared to the plan's own axis, which is
     * the right default and the wrong answer for a building that is not quite
     * square: the new room cannot be joined to walls sitting a degree or two
     * off. Rotating it is the missing move, and the grip lives on the room
     * rather than in a mode - there is nothing to arm and nothing to leave.
     *
     * Only the room's own walls turn. A wall it shares with a neighbour is
     * carried at the corner they have in common and no further, exactly as
     * moving the room already does, because the neighbour has an equal claim.
     */
    function renderRoomRotateGrip(seed: RoomSeed, face: Face): void {
        const boundary = roomBoundaryWalls(seed);
        if (!boundary || !boundary.unique.length) return;
        const pivot = interiorPoint(face.ring);
        // Clear of the room's own edge by a fixed screen distance, so the grip
        // is equally reachable on a cupboard and on a hall.
        const reach = Math.max(...face.ring.map((point) => distance(point, pivot))) + 26 * metresPerPixel();
        const axis = (state.doc.rotation_degrees * Math.PI) / 180;
        const gripAt = { x: pivot.x + Math.cos(axis - Math.PI / 2) * reach, y: pivot.y + Math.sin(axis - Math.PI / 2) * reach };

        L.polyline([toLatLng(pivot), toLatLng(gripAt)], { color: "#00838f", weight: 1, dashArray: "3 3", interactive: false }).addTo(handleLayer);
        const grip = L.circleMarker(toLatLng(gripAt), { radius: 7, color: "#00838f", fillColor: "#fff", fillOpacity: 1, weight: 2, className: "floorplan-handle floorplan-rotate-grip" }).addTo(handleLayer);

        const key = (p: Pt): string => `${p.x},${p.y}`;
        let turning: { start: number; walls: Map<Wall, { a: Pt; b: Pt }>; seedAt: Pt; ownPoints: Set<string> } | null = null;
        bindDrag(grip.getElement(), {
            move: ({ local }) => {
                if (!turning) {
                    const ownPoints = new Set<string>();
                    for (const wall of boundary.unique) {
                        ownPoints.add(key({ x: wall.ax, y: wall.ay }));
                        ownPoints.add(key({ x: wall.bx, y: wall.by }));
                    }
                    const walls = new Map<Wall, { a: Pt; b: Pt }>();
                    for (const wall of [...boundary.unique, ...boundary.shared]) walls.set(wall, { a: { x: wall.ax, y: wall.ay }, b: { x: wall.bx, y: wall.by } });
                    turning = { start: Math.atan2(local.y - pivot.y, local.x - pivot.x), walls, seedAt: { x: seed.x, y: seed.y }, ownPoints };
                    checkpoint();
                }
                const now = Math.atan2(local.y - pivot.y, local.x - pivot.x);
                // Suspending snap gives a free angle; otherwise it steps, since
                // turning a room by hand is an attempt to line it up with
                // something and the last half-degree is unhittable freehand.
                const angle = snapOff() ? now - turning.start : snapRotation(now - turning.start);
                for (const wall of boundary.unique) {
                    const origin = turning.walls.get(wall) as { a: Pt; b: Pt };
                    const a = rotate(origin.a, angle, pivot);
                    const b = rotate(origin.b, angle, pivot);
                    wall.ax = a.x;
                    wall.ay = a.y;
                    wall.bx = b.x;
                    wall.by = b.y;
                }
                for (const wall of boundary.shared) {
                    const origin = turning.walls.get(wall) as { a: Pt; b: Pt };
                    if (turning.ownPoints.has(key(origin.a))) {
                        const moved = rotate(origin.a, angle, pivot);
                        wall.ax = moved.x;
                        wall.ay = moved.y;
                    }
                    if (turning.ownPoints.has(key(origin.b))) {
                        const moved = rotate(origin.b, angle, pivot);
                        wall.bx = moved.x;
                        wall.by = moved.y;
                    }
                }
                // The seed turns with the room, so the region keeps the name it
                // was given rather than rebinding to whatever now sits over the
                // point it used to occupy.
                const movedSeed = rotate(turning.seedAt, angle, pivot);
                seed.x = movedSeed.x;
                seed.y = movedSeed.y;
                const readout = document.getElementById("floorplan-hint");
                if (readout) readout.textContent = `${Math.round((angle * 180) / Math.PI)}°`;
                render();
            },
            end: (moved) => {
                turning = null;
                if (moved) {
                    const readout = document.getElementById("floorplan-hint");
                    if (readout) readout.textContent = "";
                    markDirty();
                }
            },
        });
    }

    /** Every other wall whose endpoint exactly coincides with `point`. */
    function wallsTouchingPoint(current: Floor, point: Pt, exclude: Wall): Array<{ wall: Wall; end: "a" | "b" }> {
        const hits: Array<{ wall: Wall; end: "a" | "b" }> = [];
        for (const other of current.walls) {
            if (other === exclude) continue;
            if (other.ax === point.x && other.ay === point.y) hits.push({ wall: other, end: "a" });
            if (other.bx === point.x && other.by === point.y) hits.push({ wall: other, end: "b" });
        }
        return hits;
    }

    /**
     * Every (wall, end) reachable from `seed`'s own two endpoints by following
     * shared corners - the whole rigid network a body-drag can carry with it
     * under ALT (see the wall body's own mousedown handler). Captured once at
     * drag start, with each endpoint's *original* coordinates, so applying a
     * uniform delta later doesn't compound across ticks or double-move a
     * point reachable two different ways.
     */
    function connectedNetwork(current: Floor, seed: Wall): Array<{ wall: Wall; end: "a" | "b"; origX: number; origY: number }> {
        const visitedPoints = new Set<string>();
        const key = (p: Pt): string => `${p.x},${p.y}`;
        const queue: Pt[] = [
            { x: seed.ax, y: seed.ay },
            { x: seed.bx, y: seed.by },
        ];
        const result: Array<{ wall: Wall; end: "a" | "b"; origX: number; origY: number }> = [];
        while (queue.length) {
            const point = queue.pop() as Pt;
            if (visitedPoints.has(key(point))) continue;
            visitedPoints.add(key(point));
            for (const wall of current.walls) {
                if (wall.ax === point.x && wall.ay === point.y) {
                    result.push({ wall, end: "a", origX: wall.ax, origY: wall.ay });
                    queue.push({ x: wall.bx, y: wall.by });
                }
                if (wall.bx === point.x && wall.by === point.y) {
                    result.push({ wall, end: "b", origX: wall.bx, origY: wall.by });
                    queue.push({ x: wall.ax, y: wall.ay });
                }
            }
        }
        return result;
    }

    /**
     * Every distinct corner on a floor, with the wall ends that meet there.
     *
     * Walls store their own endpoints, so a corner shared by three walls is
     * three coordinate pairs that happen to be equal. Grouping them is what
     * turns "an endpoint" into "a joint" - the thing a user actually thinks
     * they are grabbing.
     *
     * Args:
     *     current: The floor to read.
     *
     * Returns:
     *     One entry per corner, keyed by its exact coordinates.
     */
    function wallJoints(current: Floor): Map<string, { point: Pt; ends: Array<{ wall: Wall; end: "a" | "b" }> }> {
        const joints = new Map<string, { point: Pt; ends: Array<{ wall: Wall; end: "a" | "b" }> }>();
        const add = (point: Pt, wall: Wall, end: "a" | "b"): void => {
            const key = `${point.x},${point.y}`;
            const existing = joints.get(key);
            if (existing) existing.ends.push({ wall, end });
            else joints.set(key, { point, ends: [{ wall, end }] });
        };
        for (const wall of current.walls) {
            add({ x: wall.ax, y: wall.ay }, wall, "a");
            add({ x: wall.bx, y: wall.by }, wall, "b");
        }
        return joints;
    }

    /**
     * Draw every joint on the floor, and let each one be dragged.
     *
     * Moving a joint moves exactly the walls that meet there and nothing else,
     * which is the one thing the wall-body drags cannot express. It already
     * worked, but only after selecting exactly one wall, so nobody found it -
     * and a capability nobody can find is not one the editor has. They are
     * drawn small so a plan does not turn into a field of dots, and the ones
     * belonging to the current selection are drawn large enough to aim at.
     */
    function renderJointHandles(current: Floor): void {
        const selectedWalls = new Set(state.multi.filter((item) => item.kind === "wall").map((item) => wallId(item.wall)));
        for (const joint of wallJoints(current).values()) {
            const onSelection = joint.ends.some((entry) => selectedWalls.has(wallId(entry.wall)));
            const handle = L.circleMarker(toLatLng(joint.point), {
                radius: onSelection ? 6 : 3.5,
                color: "#00838f",
                fillColor: "#fff",
                fillOpacity: 1,
                weight: onSelection ? 2 : 1,
                className: "floorplan-handle floorplan-joint",
            }).addTo(handleLayer);

            // Captured on the first move: the ends are read off the geometry as
            // it stands now, and re-reading them mid-drag would pick up walls
            // that have just been dragged onto this corner.
            let moving: Array<{ wall: Wall; end: "a" | "b" }> | null = null;
            bindDrag(handle.getElement(), {
                start: () => state.tool === "select",
                move: ({ local }) => {
                    if (!moving) {
                        moving = joint.ends;
                        checkpoint();
                    }
                    // Every wall on this joint travels with it, so none of them
                    // can be what it snaps to.
                    const carried = new Set(moving.map((entry) => wallId(entry.wall)));
                    const others = wallSegments(current).filter((segment) => !carried.has(segment.wallId));
                    const snapped = snapPoint(local, others, tolerances(), { suspended: snapOff() });
                    for (const entry of moving) {
                        if (entry.end === "a") {
                            entry.wall.ax = snapped.point.x;
                            entry.wall.ay = snapped.point.y;
                        } else {
                            entry.wall.bx = snapped.point.x;
                            entry.wall.by = snapped.point.y;
                        }
                    }
                    render();
                },
                end: (moved) => {
                    moving = null;
                    if (moved) markDirty();
                },
            });
        }
    }

    // ------------------------------------------------------------ popups

    // A click that lands elsewhere on the map while a marker's popup is open
    // closes that popup (Leaflet's own default) - without this, the very
    // same click also reached the "marker" tool's placement branch below and
    // dropped a second, unwanted marker right where the user only meant to
    // dismiss the popup. Snapshotted on mousedown, in the capture phase, so
    // it reads the state before Leaflet's own close-on-click-away logic (or
    // anything else reacting to this same mousedown) has run.
    let popupOpenCount = 0;
    map.on("popupopen", () => {
        popupOpenCount++;
    });
    map.on("popupclose", () => {
        popupOpenCount = Math.max(0, popupOpenCount - 1);
    });
    let popupOpenAtMousedown = false;
    map.getContainer().addEventListener(
        "mousedown",
        () => {
            popupOpenAtMousedown = popupOpenCount > 0;
        },
        true,
    );

    // ----------------------------------------------------------- box select

    let boxStart: L.Point | null = null;
    let boxActive = false;
    let boxRectEl: HTMLDivElement | null = null;
    // A box-select's mouseup still reaches map.on("click") below (Leaflet's
    // own drag-vs-click suppression only engages for its own panning, which
    // this never triggers since map.dragging stays disabled throughout) -
    // this flag swallows exactly that one synthetic click.
    let suppressNextClick = false;

    function drawBoxRect(from: L.Point, to: L.Point): void {
        if (!boxRectEl) return;
        const left = Math.min(from.x, to.x);
        const top = Math.min(from.y, to.y);
        boxRectEl.style.left = `${left}px`;
        boxRectEl.style.top = `${top}px`;
        boxRectEl.style.width = `${Math.abs(from.x - to.x)}px`;
        boxRectEl.style.height = `${Math.abs(from.y - to.y)}px`;
    }

    /** Items fully enclosed by the box, in plan-space regardless of screen rotation. */
    function itemsInBox(from: L.Point, to: L.Point): SelectionItem[] {
        const bounds = L.latLngBounds(map.containerPointToLatLng(from), map.containerPointToLatLng(to));
        const matches: SelectionItem[] = [];
        for (const wall of floor().walls) {
            const a = toLatLng({ x: wall.ax, y: wall.ay });
            const b = toLatLng({ x: wall.bx, y: wall.by });
            if (bounds.contains(a) && bounds.contains(b)) matches.push({ kind: "wall", wall });
        }
        for (const marker of floor().markers) {
            if (bounds.contains(toLatLng({ x: marker.x, y: marker.y }))) matches.push({ kind: "marker", marker });
        }
        for (const room of floor().rooms) {
            if (bounds.contains(toLatLng({ x: room.x, y: room.y }))) matches.push({ kind: "room", room });
        }
        return matches;
    }

    function finishBoxSelect(from: L.Point, to: L.Point, additive: boolean): void {
        const matches = itemsInBox(from, to);
        if (!matches.length) {
            if (!additive) clearSelection();
        } else if (additive) {
            for (const item of matches) if (!isSelected(item)) state.multi.push(item);
            state.selection = matches[matches.length - 1] as SelectionItem;
        } else {
            state.multi = matches;
            state.selection = matches[matches.length - 1] as SelectionItem;
        }
        renderSidebar();
        render();
    }

    // Native DOM listeners rather than Leaflet's own mouse events: this needs
    // to run *instead of* map panning (not after it), so it has to see the
    // gesture before Leaflet's drag handler decides what to do with it.
    //
    // Gated on shift, not a plain drag: a plain click-drag is already the
    // map's own pan gesture, and once an exterior is drawn, room fill covers
    // most of the visible map - a box-select startable only from genuinely
    // empty background would rarely be reachable. Shift+drag is unclaimed
    // (boxZoom above frees it) and is the same modifier most drawing tools
    // already use for exactly this.
    map.getContainer().addEventListener("mousedown", (event: MouseEvent) => {
        // Two ways in, which is the point. The Box select tool makes a plain
        // drag draw a region, the way it does in every drawing application;
        // Shift+drag does the same thing without leaving Select, for someone
        // who already knows the shortcut. Under Select alone a drag still pans,
        // because the basemap here is the document being traced and taking
        // one-finger pan away from a map is not a trade worth making.
        const viaTool = state.tool === "box";
        const viaModifier = state.tool === "select" && event.shiftKey;
        if ((!viaTool && !viaModifier) || event.button !== 0) return;
        const target = event.target as HTMLElement;
        // Ordinary wall/room/marker shapes are NOT excluded here - only
        // things with their own competing drag behavior are: draggable
        // markers and the wall/opening endpoint handles.
        if (target.closest(".leaflet-marker-icon, .floorplan-handle, .leaflet-popup, .leaflet-control, .floorplan-context-menu")) return;
        map.dragging.disable();
        boxStart = map.mouseEventToContainerPoint(event);
        boxActive = false;
    });
    map.getContainer().addEventListener("mousemove", (event: MouseEvent) => {
        if (!boxStart) return;
        const current = map.mouseEventToContainerPoint(event);
        if (!boxActive) {
            // A few pixels of slop before committing to box-select, so an
            // ordinary click on empty space (which still starts here, since
            // it might become a drag) doesn't spuriously draw a rectangle.
            if (boxStart.distanceTo(current) < 6) return;
            boxActive = true;
            map.dragging.disable();
            boxRectEl = document.createElement("div");
            boxRectEl.className = "floorplan-box-select";
            map.getContainer().appendChild(boxRectEl);
        }
        drawBoxRect(boxStart, current);
    });
    map.getContainer().addEventListener("mouseup", (event: MouseEvent) => {
        if (!boxStart) return;
        if (boxActive) {
            finishBoxSelect(boxStart, map.mouseEventToContainerPoint(event), event.ctrlKey || event.metaKey);
            suppressNextClick = true;
            boxRectEl?.remove();
            boxRectEl = null;
        }
        // Re-enabled unconditionally: mousedown above disables it as soon as
        // shift is held, before movement confirms this is really a drag.
        map.dragging.enable();
        boxStart = null;
        boxActive = false;
    });

    // Click-and-drag draws a single wall directly, instead of the map just
    // panning underneath the gesture (which is what happened before, since
    // nothing disabled map.dragging for this tool) - the existing click-
    // click-click chain for several connected walls still works unchanged,
    // since this only engages once real movement is detected, and only when
    // no chain is already in progress.
    let wallDragStartPixel: L.Point | null = null;
    let wallDragStartLocal: Pt | null = null;
    let wallDragActive = false;
    let wallDragLine: L.Polyline | null = null;
    map.getContainer().addEventListener("mousedown", (event: MouseEvent) => {
        if (state.tool !== "wall" || event.button !== 0 || state.drawing.length) return;
        const target = event.target as HTMLElement;
        if (target.closest(".leaflet-marker-icon, .floorplan-handle, .leaflet-popup, .leaflet-control, .floorplan-context-menu")) return;
        map.dragging.disable();
        wallDragStartPixel = map.mouseEventToContainerPoint(event);
        wallDragStartLocal = snapPoint(toLocal(map.containerPointToLatLng(wallDragStartPixel)), wallSegments(floor()), tolerances(), { suspended: snapOff() }).point;
        wallDragActive = false;
    });
    map.getContainer().addEventListener("mousemove", (event: MouseEvent) => {
        if (!wallDragStartPixel || !wallDragStartLocal) return;
        const current = map.mouseEventToContainerPoint(event);
        if (!wallDragActive) {
            // A few pixels of slop so an ordinary click that starts the
            // click-click chain (which also begins here, since it might
            // become a drag) doesn't spuriously draw a zero-length wall.
            if (wallDragStartPixel.distanceTo(current) < 6) return;
            wallDragActive = true;
        }
        const snapped = snapPoint(toLocal(map.containerPointToLatLng(current)), wallSegments(floor()), tolerances(), {
            from: wallDragStartLocal,
            suspended: snapOff(),
            axisRadians: (state.doc.rotation_degrees * Math.PI) / 180,
        });
        wallDragLine?.remove();
        wallDragLine = L.polyline([toLatLng(wallDragStartLocal), toLatLng(snapped.point)], { color: "#00acc1", weight: 3, dashArray: "5 5" }).addTo(ghostLayer);
    });
    map.getContainer().addEventListener("mouseup", (event: MouseEvent) => {
        if (!wallDragStartPixel || !wallDragStartLocal) return;
        if (wallDragActive) {
            const snapped = snapPoint(toLocal(map.containerPointToLatLng(map.mouseEventToContainerPoint(event))), wallSegments(floor()), tolerances(), {
                from: wallDragStartLocal,
                suspended: snapOff(),
                axisRadians: (state.doc.rotation_degrees * Math.PI) / 180,
            });
            state.drawing = [wallDragStartLocal, snapped.point];
            commitChain();
            suppressNextClick = true;
            wallDragLine?.remove();
            wallDragLine = null;
        }
        // Re-enabled unconditionally: mousedown above disables it before
        // movement confirms this is really a drag, same as box-select.
        map.dragging.enable();
        wallDragStartPixel = null;
        wallDragStartLocal = null;
        wallDragActive = false;
    });

    // ---------------------------------------------------------- context menu

    let pendingContextPoint: Pt | null = null;
    let contextMenuEl: HTMLUListElement | null = null;

    function closeContextMenu(): void {
        contextMenuEl?.remove();
        contextMenuEl = null;
    }

    function buildContextMenu(item: SelectionItem | null): HTMLUListElement {
        const menu = document.createElement("ul");
        menu.className = "floorplan-context-menu";
        const addAction = (label: string, handler: () => void): void => {
            const entry = document.createElement("li");
            const button = document.createElement("button");
            button.type = "button";
            button.textContent = label;
            button.addEventListener("click", () => {
                handler();
                closeContextMenu();
            });
            entry.appendChild(button);
            menu.appendChild(entry);
        };

        if (!item) {
            addAction(`Add ${titleCase(state.markerKind)} marker here`, () => {
                if (!pendingContextPoint) return;
                checkpoint();
                const placed: Marker = { uuid: nextLocalId(), kind: state.markerKind, x: pendingContextPoint.x, y: pendingContextPoint.y, name: titleCase(state.markerKind) };
                floor().markers.push(placed);
                state.selection = { kind: "marker", marker: placed };
                state.multi = [state.selection];
                renderSidebar();
                markDirty();
            });
            return menu;
        }

        if (item.kind === "wall") {
            // One entry per kind rather than a single "Add opening" that always
            // made a door. The type could only be changed on an opening that
            // already existed, so the word "window" appeared nowhere in the UI
            // until you had made a door and gone looking - which is why Jess
            // reported windows as unsupported when they have always been a kind.
            for (const kind of ["door", "window", "gate"] as const) {
                addAction(`Add ${kind}`, () => {
                    checkpoint();
                    item.wall.openings.push({ uuid: nextLocalId(), kind, t_start: 0.45, t_end: 0.55, swing: "none" });
                    renderSidebar();
                    markDirty();
                });
            }
        }

        const count = Math.max(state.multi.length, 1);
        addAction(count > 1 ? `Delete ${count} items` : "Delete", deleteSelection);
        return menu;
    }

    function showContextMenu(event: L.LeafletMouseEvent, item: SelectionItem | null): void {
        L.DomEvent.stop(event);
        if (item && !isSelected(item)) {
            state.multi = [item];
            state.selection = item;
            renderSidebar();
            render();
        }
        closeContextMenu();
        const menu = buildContextMenu(item);
        const original = event.originalEvent as MouseEvent;
        menu.style.left = `${original.clientX}px`;
        menu.style.top = `${original.clientY}px`;
        document.body.appendChild(menu);
        contextMenuEl = menu;
    }

    document.addEventListener("click", () => closeContextMenu());

    /**
     * Fly to whatever is already known, instead of opening on a fixed zoom
     * around the pin's coordinate.
     *
     * Preferred order: this floor's own geometry (walls, then room seeds and
     * markers if a floor somehow has those without walls), else the building
     * outline, else the fixed fallback `setView` already gave on construction
     * - a floor with nothing drawn and no known footprint has nothing to fit
     * to.
     */
    function fitToContent(): void {
        const current = floor();
        const points: Pt[] = [];
        for (const wall of current.walls) points.push({ x: wall.ax, y: wall.ay }, { x: wall.bx, y: wall.by });
        if (!points.length) for (const room of current.rooms) points.push({ x: room.x, y: room.y });
        if (!points.length) for (const marker of current.markers) points.push({ x: marker.x, y: marker.y });
        const latLngs = points.length ? points.map(toLatLng) : outline.length ? outline.map(([outlineLat, outlineLng]): [number, number] => [outlineLat, outlineLng]) : null;
        if (!latLngs) return;
        map.fitBounds(L.latLngBounds(latLngs), { padding: [32, 32], maxZoom: 22 });
    }

    /**
     * Lay the building's real footprint down as exterior walls.
     *
     * A storey's outer wall is the one part already known from survey data, so
     * asking someone to trace around a shape the map is already showing them
     * is busywork. Seeded per floor rather than shared, because upper storeys
     * genuinely differ - a setback or a demolished wing is then an edit rather
     * than a fight with something immovable.
     *
     * Args:
     *     target: The floor to lay walls on. Untouched if it already has any.
     *
     * Returns:
     *     Whether walls were added.
     */
    function seedFromOutline(target: Floor): boolean {
        if (target.walls.length || outline.length < 3) return false;
        const points = outline.map(([outlineLat, outlineLng]) => projection.toLocal({ lat: outlineLat, lng: outlineLng }));
        for (let index = 0; index < points.length; index++) {
            const from = points[index] as Pt;
            const to = points[(index + 1) % points.length] as Pt;
            if (distance(from, to) < 0.05) continue;
            target.walls.push({
                uuid: nextLocalId(),
                kind: "exterior",
                thickness: "normal",
                ax: from.x,
                ay: from.y,
                bx: to.x,
                by: to.y,
                openings: [],
            });
        }
        return target.walls.length > 0;
    }

    /**
     * Args:
     *     point: Where the seed goes, in plan-local metres.
     *     record: Whether this is a gesture of its own. False when the caller
     *         has already checkpointed, so placing a whole room is one undo
     *         step rather than two.
     */
    function addSeedAt(point: Pt, record = true): RoomSeed {
        if (record) checkpoint();
        const seed: RoomSeed = { uuid: nextLocalId(), name: "", x: point.x, y: point.y };
        floor().rooms.push(seed);
        markDirtyQuiet();
        return seed;
    }

    // --------------------------------------------------------- room tool

    const ROOM_SIDES = ["north", "south", "east", "west"] as const;
    type RoomSide = (typeof ROOM_SIDES)[number];
    type Bounds = { minX: number; maxX: number; minY: number; maxY: number };

    /**
     * Learn a room size from this floor's own already-enclosed rooms - the
     * more that exist, the closer a fresh guess tracks what this plan
     * actually looks like. Falls back to a plausible default (a small
     * bedroom) the first time, with nothing yet to learn from.
     *
     * `containerFace` - the face the new room's own center point already sits
     * inside, if any - is excluded: it is whatever the user is subdividing
     * (often the whole exterior shell), not a peer of the room being added,
     * and averaging it in would make every new room balloon to that size.
     */
    function learnedRoomSize(current: Floor, containerFace: Face | null): { width: number; height: number } {
        const DEFAULT_WIDTH = 4;
        const DEFAULT_HEIGHT = 3.5;
        const sizes: Array<{ width: number; height: number }> = [];
        for (const room of current.rooms) {
            const face = faceForSeed({ x: room.x, y: room.y }, state.faces);
            if (!face || face === containerFace) continue;
            const xs = face.ring.map((p) => p.x);
            const ys = face.ring.map((p) => p.y);
            sizes.push({ width: Math.max(...xs) - Math.min(...xs), height: Math.max(...ys) - Math.min(...ys) });
        }
        if (!sizes.length) return { width: DEFAULT_WIDTH, height: DEFAULT_HEIGHT };
        return {
            width: sizes.reduce((sum, s) => sum + s.width, 0) / sizes.length,
            height: sizes.reduce((sum, s) => sum + s.height, 0) / sizes.length,
        };
    }

    /**
     * Which compass side of its own room's bounding box a wall's midpoint
     * falls on - a shared vocabulary for "the same side" that works across
     * differently-shaped and differently-sized rooms. A wall near a corner
     * still reads as belonging to whichever axis it runs furthest out on.
     */
    function sideOfRoom(wall: Wall, bounds: Bounds): RoomSide {
        const midX = (wall.ax + wall.bx) / 2;
        const midY = (wall.ay + wall.by) / 2;
        const centerX = (bounds.minX + bounds.maxX) / 2;
        const centerY = (bounds.minY + bounds.maxY) / 2;
        const dx = (midX - centerX) / Math.max(1e-6, (bounds.maxX - bounds.minX) / 2);
        const dy = (midY - centerY) / Math.max(1e-6, (bounds.maxY - bounds.minY) / 2);
        if (Math.abs(dx) > Math.abs(dy)) return dx > 0 ? "east" : "west";
        return dy > 0 ? "south" : "north";
    }

    /**
     * The compass side existing doors most often sit on, learned the same way
     * as room size - or a random side the first time, with no doors yet to
     * learn from.
     */
    function learnedDoorSide(current: Floor, containerFace: Face | null): RoomSide {
        const tally: Record<RoomSide, number> = { north: 0, south: 0, east: 0, west: 0 };
        for (const room of current.rooms) {
            const face = faceForSeed({ x: room.x, y: room.y }, state.faces);
            if (!face || face === containerFace) continue;
            const xs = face.ring.map((p) => p.x);
            const ys = face.ring.map((p) => p.y);
            const bounds: Bounds = { minX: Math.min(...xs), maxX: Math.max(...xs), minY: Math.min(...ys), maxY: Math.max(...ys) };
            for (const id of face.wallIds) {
                const wall = current.walls.find((w) => wallId(w) === id);
                if (!wall || !wall.openings.some((o) => o.kind !== "window")) continue;
                tally[sideOfRoom(wall, bounds)]++;
            }
        }
        const best = ROOM_SIDES.reduce((a, b) => (tally[b] > tally[a] ? b : a));
        if (tally[best] > 0) return best;
        return ROOM_SIDES[Math.floor(Math.random() * ROOM_SIDES.length)] as RoomSide;
    }

    /**
     * Click-once room generation: a rectangle sized from this floor's own
     * rooms (or a sensible default), its corners snapped onto whatever
     * existing geometry sits nearby so it joins up rather than floating free,
     * with a door on whichever side existing doors tend to favor (or a random
     * one, the first time). Everything this creates is an ordinary wall or
     * opening afterward - dragging a corner, the wall's body, or the door
     * works exactly as it would for anything hand-drawn, and reusing an
     * exactly-coincident existing wall (rather than drawing a duplicate on
     * top of it) is what "joined with the nearest other walls" means here.
     */
    function placeRoomAt(center: Pt): void {
        checkpoint();
        const current = floor();
        // Whatever the click landed inside, before anything new is added -
        // usually nothing yet (open floor), sometimes the whole exterior
        // shell if it was already clicked into once. Either way it is being
        // subdivided, not matched.
        const containerFace = faceForSeed(center, state.faces);
        const { width, height } = learnedRoomSize(current, containerFace);
        const angle = (state.doc.rotation_degrees * Math.PI) / 180;
        const cos = Math.cos(angle);
        const sin = Math.sin(angle);
        const halfW = width / 2;
        const halfH = height / 2;
        const localCorners: Pt[] = [
            { x: -halfW, y: -halfH },
            { x: halfW, y: -halfH },
            { x: halfW, y: halfH },
            { x: -halfW, y: halfH },
        ];
        const rawCorners = localCorners.map((p) => ({
            x: center.x + p.x * cos - p.y * sin,
            y: center.y + p.x * sin + p.y * cos,
        }));
        const segments = wallSegments(current);
        const snapTolerance = tolerances();
        const corners = rawCorners.map((corner) => snapPoint(corner, segments, snapTolerance, { suspended: snapOff() }).point);

        const doorSide = learnedDoorSide(current, containerFace);
        const bounds: Bounds = {
            minX: Math.min(...corners.map((p) => p.x)),
            maxX: Math.max(...corners.map((p) => p.x)),
            minY: Math.min(...corners.map((p) => p.y)),
            maxY: Math.max(...corners.map((p) => p.y)),
        };

        const perimeterWalls: Wall[] = [];
        for (let i = 0; i < corners.length; i++) {
            const a = corners[i] as Pt;
            const b = corners[(i + 1) % corners.length] as Pt;
            if (distance(a, b) < 1e-6) continue; // a degenerate side once two corners snapped together
            const existing = current.walls.find((w) => (w.ax === a.x && w.ay === a.y && w.bx === b.x && w.by === b.y) || (w.ax === b.x && w.ay === b.y && w.bx === a.x && w.by === a.y));
            if (existing) {
                perimeterWalls.push(existing);
                continue;
            }
            const wall: Wall = { uuid: nextLocalId(), kind: current.walls.length === 0 ? "exterior" : "interior", thickness: "normal", ax: a.x, ay: a.y, bx: b.x, by: b.y, openings: [] };
            current.walls.push(wall);
            perimeterWalls.push(wall);
        }

        if (perimeterWalls.length) {
            // Whichever of this room's own walls best matches the learned
            // side - not necessarily doorSide exactly, since a corner snap
            // can shrink a side to nothing or merge it into a reused wall.
            let target = perimeterWalls[0] as Wall;
            for (const wall of perimeterWalls) {
                if (sideOfRoom(wall, bounds) === doorSide) {
                    target = wall;
                    break;
                }
            }
            if (!target.openings.some((o) => o.kind !== "window")) target.openings.push({ uuid: nextLocalId(), kind: "door", t_start: 0.45, t_end: 0.55, swing: "none" });
        }

        const seed = addSeedAt(center, false);
        state.selection = { kind: "room", room: seed };
        state.multi = [state.selection];
        setTool("select");
        markDirty();
    }

    // ------------------------------------------------------------- drawing

    function commitChain(): void {
        const points = state.drawing;
        if (points.length >= 2) {
            checkpoint();
            const kind = state.wallKind;
            for (let i = 0; i < points.length - 1; i++) {
                const a = points[i] as Pt;
                const b = points[i + 1] as Pt;
                if (distance(a, b) < 1e-6) continue;
                floor().walls.push({
                    uuid: nextLocalId(),
                    // The first wall on an empty floor is the shell whatever the
                    // panel says - nobody starts a plan with an interior
                    // partition - and after that the panel decides.
                    kind: floor().walls.length === 0 ? "exterior" : kind,
                    thickness: "normal",
                    ax: a.x,
                    ay: a.y,
                    bx: b.x,
                    by: b.y,
                    openings: [],
                });
            }
            state.dirty = true;
            queueAutosave();
        }
        state.drawing = [];
        ghostLayer.clearLayers();
        render();
    }

    function drawGhost(): void {
        ghostLayer.clearLayers();
        if (!state.drawing.length) return;
        const points = [...state.drawing];
        if (state.cursor) points.push(state.cursor);
        L.polyline(points.map(toLatLng), { color: "#00acc1", weight: 3, dashArray: "5 5" }).addTo(ghostLayer);
        for (const p of state.drawing) {
            L.circleMarker(toLatLng(p), { radius: 4, color: "#00acc1", fillColor: "#fff", fillOpacity: 1 }).addTo(ghostLayer);
        }
        const last = state.drawing[state.drawing.length - 1] as Pt;
        if (state.cursor) {
            const metres = distance(last, state.cursor);
            L.marker(toLatLng(state.cursor), {
                icon: L.divIcon({
                    className: "floorplan-measure",
                    html: `${metres.toFixed(2)} m${state.snapKind ? ` · ${state.snapKind}` : ""}`,
                }),
                interactive: false,
            }).addTo(ghostLayer);
        }
    }

    map.on("mousemove", (event: L.LeafletMouseEvent) => {
        if (state.tool !== "wall") return;
        const raw = toLocal(event.latlng);
        const from = state.drawing.length ? (state.drawing[state.drawing.length - 1] as Pt) : null;
        const snapped = snapPoint(raw, wallSegments(floor()), tolerances(), {
            from,
            suspended: snapOff(),
            axisRadians: (state.doc.rotation_degrees * Math.PI) / 180,
        });
        state.cursor = snapped.point;
        state.snapKind = snapped.label;
        drawGhost();
    });

    map.on("click", (event: L.LeafletMouseEvent) => {
        // A box-select drag ends in a mouseup that Leaflet still reads as a
        // click (map.dragging was never engaged, so its usual after-a-drag
        // click suppression never kicks in) - without this the box-select
        // result would be immediately wiped by the "click empty space
        // deselects" branch below.
        if (suppressNextClick) {
            suppressNextClick = false;
            return;
        }
        if (popupOpenAtMousedown) {
            popupOpenAtMousedown = false;
            return;
        }
        const raw = toLocal(event.latlng);
        if (state.tool === "wall") {
            const from = state.drawing.length ? (state.drawing[state.drawing.length - 1] as Pt) : null;
            const snapped = snapPoint(raw, wallSegments(floor()), tolerances(), {
                from,
                suspended: snapOff(),
                axisRadians: (state.doc.rotation_degrees * Math.PI) / 180,
            });
            // Clicking the chain's own origin closes the loop and finishes.
            const origin = state.drawing[0];
            const closeTolerance = 12 * metresPerPixel();
            if (origin && state.drawing.length >= 2 && distance(snapped.point, origin) < closeTolerance) {
                state.drawing.push(origin);
                commitChain();
                return;
            }
            // Clicking the chain's own last point again finishes it open-ended
            // (no closing segment) - the same thing double-click already
            // does, offered as a second click on the same spot too, since
            // that is an easy thing to reach for without the map having
            // registered it as an actual double-click.
            const last = state.drawing[state.drawing.length - 1];
            if (last && state.drawing.length >= 2 && distance(snapped.point, last) < closeTolerance) {
                commitChain();
                return;
            }
            state.drawing.push(snapped.point);
            drawGhost();
            return;
        }
        if (state.tool === "room") {
            placeRoomAt(raw);
            return;
        }
        if (state.tool === "opening") {
            const near = wallNear(raw);
            if (!near) {
                toast.info("Tap a wall to cut an opening into it.");
                return;
            }
            checkpoint();
            const length = wallLength(near.wall);
            // A fixed 0.9m, not a fixed fraction of the wall: a door is a door
            // whether it is in a 2m partition or a 12m elevation, and the
            // fraction that used to be hardcoded made it neither.
            const width = Math.min(0.9, length * 0.9) / length;
            const centre = projectOnSegment(raw, { x: near.wall.ax, y: near.wall.ay }, { x: near.wall.bx, y: near.wall.by }).t;
            const [start, end] = clampOpening(centre - width / 2, centre + width / 2);
            const cut: Opening = { uuid: nextLocalId(), kind: state.openingKind, t_start: start, t_end: end, swing: "none" };
            near.wall.openings.push(cut);
            state.selection = { kind: "opening", wall: near.wall, opening: cut };
            state.multi = [state.selection];
            renderSidebar();
            markDirty();
            return;
        }
        if (state.tool === "marker") {
            // Select what was just placed: naming it, or linking a stair to the
            // floor below, is almost always the next thing wanted, and making
            // the user hunt for and click their own new marker to do it is a
            // step with no purpose.
            // Pre-filled with the type as text - almost always exactly what
            // someone wants ("Hazard"), and cheaper to edit than to type from
            // scratch when it is not.
            checkpoint();
            const placed: Marker = { uuid: nextLocalId(), kind: state.markerKind, x: raw.x, y: raw.y, name: titleCase(state.markerKind) };
            floor().markers.push(placed);
            state.selection = { kind: "marker", marker: placed };
            state.multi = [state.selection];
            renderSidebar();
            markDirty();
            return;
        }
        clearSelection();
        renderSidebar();
        render();
    });

    map.on("dblclick", () => {
        if (state.tool === "wall") commitChain();
    });

    map.on("contextmenu", (event: L.LeafletMouseEvent) => {
        if (state.tool !== "select") return;
        pendingContextPoint = toLocal(event.latlng);
        showContextMenu(event, null);
    });

    // The same value drives wall angle-snapping (see the wall tool's
    // mousemove/click handlers above) and the map's own visual rotation, so
    // turning the map to face a building also squares the snap grid to it -
    // rotating and drawing stay in agreement instead of one silently
    // fighting the other. No render() here: leaflet-rotate already
    // repositions every existing layer itself; redrawing would just be
    // wasted work on every frame of a drag or two-finger twist.
    // Dragging anywhere while the rotate tool is armed turns the view. Bound on
    // the container rather than a layer: the whole canvas is the handle, which
    // is the entire difference from the control this replaced.
    map.getContainer().addEventListener("pointerdown", (raw) => {
        const event = raw as PointerEvent;
        if (state.tool !== "rotate") return;
        if (event.pointerType === "mouse" && event.button !== 0) return;
        const centre = map.getSize().divideBy(2);
        const angleAt = (source: PointerEvent): number => {
            const point = map.mouseEventToContainerPoint(source);
            return (Math.atan2(point.y - centre.y, point.x - centre.x) * 180) / Math.PI;
        };
        const startAngle = angleAt(event);
        const startBearing = map.getBearing();
        map.dragging.disable();
        const surface = map.getContainer();
        try {
            surface.setPointerCapture(event.pointerId);
        } catch {
            // Best effort; the window listeners below carry the gesture anyway.
        }
        let turned = false;
        const onMove = (moveRaw: Event): void => {
            const moveEvent = moveRaw as PointerEvent;
            if (moveEvent.pointerId !== event.pointerId) return;
            turned = true;
            map.setBearing(startBearing + (angleAt(moveEvent) - startAngle));
        };
        let done = false;
        const onFinish = (): void => {
            if (done) return;
            done = true;
            window.removeEventListener("pointermove", onMove);
            window.removeEventListener("pointerup", onFinish);
            window.removeEventListener("pointercancel", onFinish);
            map.dragging.enable();
            // The release reads as a click on the canvas, which would otherwise
            // fall through to "clicked empty space" and drop the selection -
            // so turning the plan to look at something would deselect it.
            if (turned) suppressNextClick = true;
        };
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onFinish);
        window.addEventListener("pointercancel", onFinish);
    });

    // A zoom changes what fits without changing anything worth re-rendering.
    map.on("zoomend", () => scheduleRoomLabelFit());

    map.on("rotate", () => {
        const bearing = map.getBearing();
        // Also fires for the load-time setBearing() that restores a saved
        // plan's rotation - a degrees-to-radians-and-back round trip through
        // Leaflet's own storage can perturb the value by float noise even
        // when nothing really changed, so this compares loosely rather than
        // with ===, which would mark a freshly-loaded plan dirty before any
        // real edit happened.
        if (Math.abs(bearing - state.doc.rotation_degrees) < 1e-6) return;
        checkpoint("rotate");
        state.doc.rotation_degrees = bearing;
        markDirtyQuiet();
    });

    document.addEventListener("keydown", (event) => {
        const target = event.target as HTMLElement | null;
        // Every hotkey below is a bare letter (mnemonics: W for Wall, and so
        // on) - without this, typing a room name like "Waiting room" fires
        // the Wall tool on its own "w". The old 1/2/3 keys never had this
        // problem (plan names rarely contain a digit), which is presumably
        // why it went unnoticed until letters were added alongside them.
        const typing = !!(target && ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));

        // Backtick, not Alt: Alt now means "take less" for a drag, and a key
        // that both detaches a wall and disables snapping is a key whose effect
        // nobody can predict. Bare, so it works mid-drag - snapping is not
        // latched, only the mode is.
        if (event.key === "`") state.suspendSnap = true;
        if (event.key === "Escape") {
            closeContextMenu();
            if (state.drawing.length) commitChain();
            // Escape leaves a tool before it clears a selection: an armed tool
            // is the more surprising state to be stuck in, and it is the one a
            // user reaches for Escape to get out of.
            else if (state.tool !== "select") setTool("select");
            else {
                clearSelection();
                renderSidebar();
                render();
            }
        }
        if ((event.key === "Delete" || event.key === "Backspace") && state.selection) {
            if (typing) return;
            event.preventDefault();
            deleteSelection();
        }
        if (typing) return;
        const key = event.key.toLowerCase();
        if (key === "z" && (event.ctrlKey || event.metaKey)) {
            event.preventDefault();
            if (event.shiftKey) redo();
            else undo();
            return;
        }
        if (key === "y" && (event.ctrlKey || event.metaKey)) {
            event.preventDefault();
            redo();
            return;
        }
        if (key === "1" || key === "v") setTool("select");
        if (key === "b") setTool("box");
        if (key === "d") setTool("opening");
        if (key === "t") setTool("rotate");
        if (key === "2" || key === "w") setTool("wall");
        if (key === "r") setTool("room");
        if (key === "3" || key === "m") setTool("marker");
        if (key === "h") {
            state.markerKind = "hazard";
            setTool("marker");
        }
        if (key === "s") {
            state.markerKind = "stair";
            setTool("marker");
        }
        if (key === "e") {
            state.markerKind = "elevator";
            setTool("marker");
        }
    });
    document.addEventListener("keyup", (event) => {
        if (event.key === "`") state.suspendSnap = false;
    });

    function deleteSelection(): void {
        const current = floor();
        const targets: SelectionItem[] = state.multi.length ? state.multi : state.selection ? [state.selection] : [];
        if (!targets.length) return;
        checkpoint();
        // Captured before anything is removed: afterwards there is no way to
        // tell a seed that just lost its room from one that never had one.
        const boundBefore = boundSeeds(current);
        const walls = new Set(targets.filter((t): t is Extract<SelectionItem, { kind: "wall" }> => t.kind === "wall").map((t) => t.wall));
        const rooms = new Set(targets.filter((t): t is Extract<SelectionItem, { kind: "room" }> => t.kind === "room").map((t) => t.room));
        const markers = new Set(targets.filter((t): t is Extract<SelectionItem, { kind: "marker" }> => t.kind === "marker").map((t) => t.marker));
        if (walls.size) current.walls = current.walls.filter((w) => !walls.has(w));
        if (rooms.size) current.rooms = current.rooms.filter((r) => !rooms.has(r));
        if (markers.size) current.markers = current.markers.filter((m) => !markers.has(m));
        for (const target of targets) {
            if (target.kind === "opening") target.wall.openings = target.wall.openings.filter((o) => o !== target.opening);
        }
        if (walls.size) pruneOrphanedSeeds(current, boundBefore);
        clearSelection();
        renderSidebar();
        markDirty();
    }

    /**
     * Drop the room seeds a deletion has just orphaned.
     *
     * Two seeds can both be unbound and mean opposite things. One was placed
     * in a region the author has not closed yet: that is a promise to finish,
     * and it is shown as a hint. The other belonged to a room that existed
     * until its walls were deleted a moment ago: nothing is coming back for
     * it, and leaving it behind puts a dot on the map labelled with a room
     * that is gone.
     *
     * The difference is not visible in the seed - only in what just happened -
     * so it has to be decided here, against the set that was bound before the
     * deletion.
     *
     * Args:
     *     current: The floor whose seeds should be reconsidered.
     *     boundBefore: The seeds that were part of an enclosed room when the
     *         gesture started.
     */
    function pruneOrphanedSeeds(current: Floor, boundBefore: ReadonlySet<RoomSeed>): void {
        if (!current.rooms.length) return;
        const faces = deriveFaces(wallSegments(current)).faces;
        const orphaned = current.rooms.filter((room) => boundBefore.has(room) && !faceForSeed({ x: room.x, y: room.y }, faces));
        if (!orphaned.length) return;
        current.rooms = current.rooms.filter((room) => !orphaned.includes(room));
        if (state.selection?.kind === "room" && orphaned.includes(state.selection.room)) clearSelection();
    }

    /** The seeds currently sitting inside a derived room, before an edit. */
    function boundSeeds(current: Floor): Set<RoomSeed> {
        return new Set(current.rooms.filter((room) => faceForSeed({ x: room.x, y: room.y }, state.faces)));
    }

    /**
     * What the armed tool will do next, shown beside the tools.
     *
     * The alternative is modifier keys, and modifiers cannot be seen, cannot be
     * discovered and do not exist on a phone at all. Everything here is a
     * visible control first; the keyboard shortcuts are accelerators for these,
     * not the only way to reach them.
     */
    function renderToolOptions(): void {
        const host = document.getElementById("floorplan-tool-options");
        if (!host) return;
        host.replaceChildren();

        const group = <T extends string>(label: string, options: ReadonlyArray<{ value: T; label: string }>, current: T, onPick: (value: T) => void): void => {
            const wrap = document.createElement("div");
            wrap.className = "floorplan-tool-options__group";
            const title = document.createElement("span");
            title.className = "floorplan-tool-options__label";
            title.textContent = label;
            wrap.appendChild(title);
            for (const option of options) {
                const button = document.createElement("button");
                button.type = "button";
                button.className = `btn btn--sm${option.value === current ? " btn--primary" : " btn--ghost"}`;
                button.textContent = option.label;
                button.setAttribute("aria-pressed", String(option.value === current));
                button.addEventListener("click", () => {
                    onPick(option.value);
                    renderToolOptions();
                });
                wrap.appendChild(button);
            }
            host.appendChild(wrap);
        };

        if (state.tool === "wall") {
            group("New walls", WALL_KINDS, state.wallKind, (value) => {
                state.wallKind = value;
            });
        }
        if (state.tool === "opening") {
            group("Cut a", OPENING_KINDS, state.openingKind, (value) => {
                state.openingKind = value;
            });
        }
        if (state.tool === "marker") {
            group(
                "Marker",
                (Object.keys(MARKER_ICON) as MarkerKind[]).map((kind) => ({ value: kind, label: titleCase(kind) })),
                state.markerKind,
                (value) => {
                    state.markerKind = value;
                },
            );
        }

        // Snapping applies to every tool that puts a point somewhere, so it is
        // the one control that stays put rather than changing with the tool.
        if (state.tool !== "rotate" && state.tool !== "box") {
            const wrap = document.createElement("div");
            wrap.className = "floorplan-tool-options__group";
            const toggle = document.createElement("button");
            toggle.type = "button";
            toggle.className = `btn btn--sm${state.snapEnabled ? " btn--primary" : " btn--ghost"}`;
            toggle.setAttribute("aria-pressed", String(state.snapEnabled));
            toggle.innerHTML = '<i class="material-symbols-outlined">grid_on</i> Snap';
            toggle.addEventListener("click", () => {
                state.snapEnabled = !state.snapEnabled;
                renderToolOptions();
            });
            wrap.appendChild(toggle);
            host.appendChild(wrap);
        }

        host.hidden = host.childElementCount === 0;
    }

    function setTool(tool: Tool): void {
        if (state.drawing.length) commitChain();
        state.tool = tool;
        for (const button of document.querySelectorAll<HTMLButtonElement>("#floorplan-tools button")) {
            button.classList.toggle("is-active", button.dataset.tool === tool);
            button.setAttribute("aria-pressed", String(button.dataset.tool === tool));
        }
        mapEl.classList.toggle("is-drawing", tool !== "select" && tool !== "box" && tool !== "rotate");
        mapEl.classList.toggle("is-boxing", tool === "box");
        mapEl.classList.toggle("is-rotating", tool === "rotate");
        const hint = document.getElementById("floorplan-hint");
        if (hint) {
            // Select needs no hint - the interaction is the ordinary "click a
            // thing to work on it" every other tool on the site already uses.
            hint.textContent =
                tool === "box"
                    ? "Drag a region to select everything inside it · Esc returns to Select"
                    : tool === "rotate"
                      ? "Drag anywhere to turn the plan · Esc returns to Select"
                      : tool === "opening"
                        ? "Tap a wall to cut the opening showing above"
                        : tool === "wall"
                    ? "Click to place corners · click the first corner to close · Esc finishes · hold ` to ignore snapping"
                    : tool === "room"
                      ? "Click to generate a rectangular room, sized and joined from what's already drawn"
                        : tool === "marker"
                          ? "Click to drop a marker"
                          : "";
        }
        renderToolOptions();
        renderSidebar();
    }

    // ------------------------------------------------------------- sidebar

    /**
     * Editable fields for whichever floor is showing.
     *
     * Two fields rather than one, sitting under the strip where they can be
     * seen: a floor's code and its nickname are different facts, and the
     * previous single prompt could only ever set one of them - which is how
     * naming a floor came to destroy the record of which storey it was.
     *
     * The code field is left blank when the label is derived, with the derived
     * value as its placeholder. That way an empty box reads as "this follows
     * the stack", and clearing a code goes back to following it, without
     * either state needing a caption.
     */
    function renderFloorFields(host: HTMLElement, item: Floor): void {
        const labels = floorLabels();
        const row = document.createElement("div");
        row.className = "floorplan-floor-fields";

        const code = document.createElement("input");
        code.className = "form-input floorplan-floor-fields__code";
        code.value = item.designation || "";
        code.placeholder = labels.get(item) || "";
        code.maxLength = 8;
        code.setAttribute("aria-label", "Floor number or code");
        code.addEventListener("input", () => {
            checkpoint(`floor-code:${item.uuid || item.level}`);
            item.designation = code.value.trim().slice(0, 8);
            markDirtyQuiet();
        });
        // Re-rendered on commit, not per keystroke: every other floor's derived
        // label can change as a result of this one, and rebuilding the strip
        // under the cursor would take the focus with it.
        code.addEventListener("change", () => renderSidebar());
        row.appendChild(code);

        const nickname = document.createElement("input");
        nickname.className = "form-input floorplan-floor-fields__name";
        nickname.value = item.name || "";
        nickname.placeholder = "Nickname";
        nickname.setAttribute("aria-label", "Floor nickname");
        nickname.addEventListener("input", () => {
            checkpoint(`floor-name:${item.uuid || item.level}`);
            item.name = nickname.value;
            markDirtyQuiet();
        });
        nickname.addEventListener("change", () => renderSidebar());
        row.appendChild(nickname);

        host.appendChild(row);
    }

    /** Copy one floor's walls (and optionally its room names) onto another. */
    function duplicateFloor(source: Floor): void {
        checkpoint();
        const level = Math.max(...state.doc.floors.map((item) => item.level)) + 1;
        const added: Floor = { level, name: source.name, designation: "", walls: [], rooms: [], markers: [] };
        const copied = copyFloorContents(source, { rooms: true, markers: false });
        added.walls = copied.walls;
        added.rooms = copied.rooms;
        state.doc.floors.push(added);
        normaliseFloors(added);
        markDirty();
        renderSidebar();
        fitToContent();
    }

    function deleteFloor(index: number): void {
        const item = state.doc.floors[index] as Floor;
        if (state.doc.floors.length <= 1) return;
        // Nothing to lose yet - the warning exists to protect drawn work, not
        // to make deleting a floor someone opened by mistake a two-step chore.
        const isEmpty = !item.walls.length && !item.rooms.length && !item.markers.length;
        if (!isEmpty && !window.confirm(`Delete "${item.name || floorLabels().get(item) || `Level ${item.level}`}"? This removes everything drawn on it.`)) return;
        checkpoint();
        state.doc.floors.splice(index, 1);
        state.floorIndex = Math.min(state.floorIndex, state.doc.floors.length - 1);
        // Otherwise the stack reads "1, 2, 4": the storey above a deleted one
        // keeps a level nothing sits below any more, and "the floor below"
        // starts meaning a storey two down.
        normaliseFloors(state.doc.floors[state.floorIndex] as Floor | undefined || null);
        clearSelection();
        renderSidebar();
        markDirty();
    }

    /**
     * Keep the stack ordered by level, contiguous, and pointing at the same
     * storey it was before.
     *
     * Everything structural reads adjacency off ``level``: the floor-below
     * underlay, connector linking, and the server's own ordering. A gap left
     * by deleting a middle floor makes "the floor below" mean a storey that
     * is two down, so the repair happens at every mutation rather than being
     * left for someone to notice.
     *
     * Args:
     *     keep: The floor that should still be selected afterwards. Defaults
     *         to whichever is selected now.
     */
    function normaliseFloors(keep: Floor | null = null): void {
        const active = keep || (state.doc.floors[state.floorIndex] as Floor | undefined) || null;
        const repaired = contiguousLevels(state.doc.floors);
        for (const { floor: item, level } of repaired) item.level = level;
        state.doc.floors = repaired.map((entry) => entry.floor);
        const index = active ? state.doc.floors.indexOf(active) : -1;
        state.floorIndex = index >= 0 ? index : Math.min(state.floorIndex, state.doc.floors.length - 1);
    }

    /** The lift-button label for each floor, derived from the stack. */
    function floorLabels(): Map<Floor, string> {
        return deriveDesignations(state.doc.floors);
    }

    /**
     * Start a new floor from the nearest existing one's shell.
     *
     * A storey's exterior is almost always the storey below's exterior, and
     * re-tracing it by hand for every floor is the bulk of the work in a
     * multi-storey building.
     */
    function seedShellFrom(target: Floor): boolean {
        const others = state.doc.floors.filter((item) => item !== target && item.walls.some((wall) => wall.kind === "exterior"));
        if (!others.length) return false;
        const nearest = others.reduce((best, item) => (Math.abs(item.level - target.level) < Math.abs(best.level - target.level) ? item : best));
        const shell: Floor = { ...nearest, walls: nearest.walls.filter((wall) => wall.kind === "exterior") };
        const copied = copyFloorContents(shell, { rooms: false, markers: false });
        target.walls.push(...copied.walls);
        return copied.walls.length > 0;
    }

    function renderFloorTabs(): void {
        const host = document.getElementById("floorplan-floors");
        if (!host) return;
        host.replaceChildren();
        const labels = floorLabels();
        // Bottom of the building at the bottom of the strip.
        [...state.doc.floors].reverse().forEach((item) => {
            const index = state.doc.floors.indexOf(item);
            const tab = document.createElement("span");
            tab.className = "floorplan-floor-tab";
            const button = document.createElement("button");
            button.type = "button";
            button.className = `btn btn--sm${index === state.floorIndex ? " btn--primary" : " btn--ghost"}`;
            // The designation always shows, even for a floor with a nickname.
            // Renaming a storey used to replace the only thing that said which
            // storey it was, so a plan of renamed floors could not be read.
            const chip = document.createElement("span");
            chip.className = "floorplan-floor-tab__chip";
            chip.textContent = labels.get(item) || String(item.level);
            button.appendChild(chip);
            if (item.name) {
                const nickname = document.createElement("span");
                nickname.className = "floorplan-floor-tab__name";
                nickname.textContent = item.name;
                button.appendChild(nickname);
            }
            button.addEventListener("click", () => {
                if (index === state.floorIndex) return;
                state.floorIndex = index;
                clearSelection();
                renderSidebar();
                render();
                fitToContent();
            });
            tab.appendChild(button);
            if (state.doc.floors.length > 1) {
                const remove = document.createElement("button");
                remove.type = "button";
                remove.className = "btn btn--icon-sm floorplan-floor-tab__delete";
                remove.innerHTML = '<i class="material-symbols-outlined">close</i>';
                remove.setAttribute("aria-label", `Delete floor ${labels.get(item) || item.level}`);
                remove.addEventListener("click", (event) => {
                    event.stopPropagation();
                    deleteFloor(index);
                });
                tab.appendChild(remove);
            }
            host.appendChild(tab);
        });
        const add = document.createElement("button");
        add.type = "button";
        add.className = "btn btn--sm btn--ghost";
        add.textContent = "+ Floor";
        add.addEventListener("click", () => {
            checkpoint();
            const levels = state.doc.floors.map((item) => item.level);
            const level = (levels.length ? Math.max(...levels) : -1) + 1;
            const added: Floor = { level, name: "", walls: [], rooms: [], markers: [] };
            state.doc.floors.push(added);
            normaliseFloors(added);
            // The storey below's shell first, the building outline only when
            // there is no storey below to copy - a plan's upper floors follow
            // its own exterior, not the provider's footprint.
            if (!seedShellFrom(added)) seedFromOutline(added);
            markDirty();
            renderSidebar();
        });
        host.appendChild(add);

        const duplicate = document.createElement("button");
        duplicate.type = "button";
        duplicate.className = "btn btn--sm btn--ghost";
        duplicate.innerHTML = '<i class="material-symbols-outlined">content_copy</i>';
        duplicate.setAttribute("aria-label", "Duplicate this floor");
        duplicate.addEventListener("click", () => duplicateFloor(floor()));
        host.appendChild(duplicate);

        renderFloorFields(host, floor());
    }

    function field(labelText: string, input: HTMLElement): HTMLLabelElement {
        const label = document.createElement("label");
        label.className = "floorplan-field";
        const span = document.createElement("span");
        span.textContent = labelText;
        label.append(span, input);
        return label;
    }

    function select(options: ReadonlyArray<{ value: string; label: string }>, value: string, onChange: (v: string) => void): HTMLSelectElement {
        const node = document.createElement("select");
        node.className = "form-input";
        for (const option of options) {
            const item = document.createElement("option");
            item.value = option.value;
            item.textContent = option.label;
            if (option.value === value) item.selected = true;
            node.appendChild(item);
        }
        node.addEventListener("change", () => {
            checkpoint();
            onChange(node.value);
        });
        return node;
    }

    /**
     * The fields every item has, whatever kind of thing it is.
     *
     * Walls, openings, rooms and markers all inherit the same surface on the
     * server - description, condition, an open attribute bag - and none of it
     * was reachable. It is one shared block rather than four per-type forms so
     * that a field added here appears everywhere at once, which is the whole
     * reason the model puts them on a common base.
     *
     * Folded away by default. Most of the time someone is drawing walls, not
     * annotating them, and a form that is always open makes the common case
     * read as the unusual one.
     *
     * Args:
     *     host: Where to append.
     *     item: The selected item, mutated in place as fields change.
     *     key: Stable identity for the item, so a run of keystrokes in one
     *         field collapses into a single undo step.
     */
    function renderItemDetails(host: HTMLElement, item: ItemDetails, key: string): void {
        const filled = Boolean(item.description || item.condition || attribute(item, "material"));
        const box = document.createElement("details");
        box.className = "floorplan-details";
        // Already-annotated items open on selection: the disclosure is there to
        // keep empty fields out of the way, not to hide what someone wrote.
        box.open = filled;
        const summary = document.createElement("summary");
        summary.textContent = "Details";
        box.appendChild(summary);

        const text = (label: string, value: string, placeholder: string, onInput: (next: string) => void): void => {
            const input = document.createElement("input");
            input.className = "form-input";
            input.type = "text";
            input.value = value;
            input.placeholder = placeholder;
            input.addEventListener("input", () => {
                checkpoint(`${key}:${label}`);
                onInput(input.value);
                markDirtyQuiet();
            });
            box.appendChild(field(label, input));
        };

        text("Material", attribute(item, "material"), "Brick, timber, breeze block", (next) => setAttribute(item, "material", next));
        text("Condition", item.condition || "", "Sound, rotten, part-collapsed", (next) => {
            item.condition = next;
        });

        const notes = document.createElement("textarea");
        notes.className = "form-input";
        notes.rows = 3;
        notes.value = item.description || "";
        notes.placeholder = "Anything worth remembering about this";
        notes.addEventListener("input", () => {
            checkpoint(`${key}:description`);
            item.description = notes.value;
            markDirtyQuiet();
        });
        box.appendChild(field("Notes", notes));

        host.appendChild(box);
    }

    /**
     * Show the shared icon and colour controls for the selected marker.
     *
     * The controls are server-rendered once in the page and moved into view
     * rather than rebuilt per selection: the icon set and the palette live in
     * Python, and a copy of either here would be a second list to keep in step
     * with the pin detail page - which is the thing this is meant to prevent.
     *
     * Appearance is stored on the marker's linked detail pin, not on the
     * marker, so a marker styled here and the same pin styled from the pin page
     * are editing one value.
     *
     * Args:
     *     marker: The selected marker, or null to hide the controls.
     */
    function renderMarkerAppearance(marker: Marker | null): void {
        const host = document.getElementById("floorplan-marker-appearance");
        if (!host) return;
        host.hidden = marker === null;
        if (!marker) return;

        const iconInput = document.getElementById("icon-value-floorplan-marker") as HTMLInputElement | null;
        if (iconInput) {
            iconInput.value = marker.icon || "";
            iconInput.onchange = () => {
                checkpoint();
                marker.icon = iconInput.value || null;
                markDirty();
            };
        }

        for (const swatch of host.querySelectorAll<HTMLButtonElement>(".floorplan-swatch")) {
            const colour = swatch.dataset.color || "";
            swatch.classList.toggle("is-active", (marker.color || "") === colour);
            swatch.onclick = () => {
                checkpoint();
                marker.color = colour || null;
                markDirty();
                renderSidebar();
            };
        }
    }

    function renderSidebar(): void {
        const host = document.getElementById("floorplan-form");
        if (!host) return;
        host.replaceChildren();
        const selection = state.selection;
        renderMarkerAppearance(selection && selection.kind === "marker" && state.multi.length === 1 ? selection.marker : null);
        if (!selection) return;

        // More than one item selected: a per-kind edit form doesn't apply,
        // so offer only what makes sense in bulk - a shared "Type" for an
        // all-walls selection, and delete, which always applies.
        if (state.multi.length > 1) {
            const heading = document.createElement("h3");
            heading.textContent = `${state.multi.length} items selected`;
            host.appendChild(heading);
            if (state.multi.every((item) => item.kind === "wall")) {
                const typeSelect = document.createElement("select");
                typeSelect.className = "form-input";
                const placeholder = document.createElement("option");
                placeholder.value = "";
                placeholder.textContent = "Set type for all…";
                typeSelect.appendChild(placeholder);
                for (const kind of WALL_KINDS.map((entry) => entry.value)) {
                    const item = document.createElement("option");
                    item.value = kind;
                    item.textContent = kind;
                    typeSelect.appendChild(item);
                }
                typeSelect.addEventListener("change", () => {
                    if (!typeSelect.value) return;
                    checkpoint();
                    for (const item of state.multi) if (item.kind === "wall") item.wall.kind = typeSelect.value as Wall["kind"];
                    markDirty();
                });
                host.appendChild(field("Type", typeSelect));
            }
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "btn btn--sm btn--danger";
            remove.textContent = `Delete ${state.multi.length} items`;
            remove.addEventListener("click", deleteSelection);
            host.appendChild(remove);
            return;
        }

        if (selection.kind === "wall") {
            const wall = selection.wall;
            const heading = document.createElement("h3");
            heading.textContent = `Wall · ${wallLength(wall).toFixed(2)} m`;
            host.appendChild(heading);
            host.appendChild(
                field(
                    "Type",
                    select(WALL_KINDS, wall.kind, (v) => {
                        wall.kind = v as Wall["kind"];
                        markDirty();
                    }),
                ),
            );
            host.appendChild(
                field(
                    "Thickness",
                    select(
                        [
                            { value: "thin", label: "Thin" },
                            { value: "normal", label: "Normal" },
                            { value: "thick", label: "Thick" },
                        ],
                        wall.thickness,
                        (v) => {
                            wall.thickness = v as Wall["thickness"];
                            markDirty();
                        },
                    ),
                ),
            );

            const openings = document.createElement("div");
            openings.className = "floorplan-openings";
            const title = document.createElement("span");
            title.className = "floorplan-field__label";
            title.textContent = "Openings";
            openings.appendChild(title);
            wall.openings.forEach((opening, index) => {
                const row = document.createElement("div");
                row.className = "floorplan-opening-row";
                row.append(
                    select(OPENING_KINDS, opening.kind, (v) => {
                        opening.kind = v as typeof opening.kind;
                        markDirty();
                    }),
                );
                const remove = document.createElement("button");
                remove.type = "button";
                remove.className = "btn btn--icon-sm btn--danger";
                remove.innerHTML = '<i class="material-symbols-outlined">close</i>';
                remove.addEventListener("click", () => {
                    wall.openings.splice(index, 1);
                    renderSidebar();
                    markDirty();
                });
                row.appendChild(remove);
                openings.appendChild(row);
            });
            const addOpening = document.createElement("button");
            addOpening.type = "button";
            addOpening.className = "btn btn--sm btn--ghost";
            addOpening.textContent = "Add opening";
            addOpening.addEventListener("click", () => {
                // Placed mid-wall at a tenth of its length; drag handles come later.
                wall.openings.push({ uuid: nextLocalId(), kind: "door", t_start: 0.45, t_end: 0.55, swing: "none" });
                renderSidebar();
                markDirty();
            });
            openings.appendChild(addOpening);
            host.appendChild(openings);
            renderAxisControl(host, wall);
        }

        if (selection.kind === "room") {
            const room = selection.room;
            const heading = document.createElement("h3");
            heading.textContent = "Room";
            host.appendChild(heading);
            const name = document.createElement("input");
            name.className = "form-input";
            name.value = room.name;
            name.placeholder = "Boiler room";
            name.addEventListener("input", () => {
                checkpoint(`room-name:${room.uuid}`);
                room.name = name.value;
                markDirtyQuiet();
            });
            name.addEventListener("change", () => render());
            host.appendChild(field("Name", name));
        }

        if (selection.kind === "marker") {
            const marker = selection.marker;
            const heading = document.createElement("h3");
            heading.textContent = "Marker";
            host.appendChild(heading);
            const name = document.createElement("input");
            name.className = "form-input";
            name.value = marker.name || "";
            name.addEventListener("input", () => {
                checkpoint(`marker-name:${marker.uuid}`);
                marker.name = name.value;
                markDirtyQuiet();
            });
            host.appendChild(field("Label", name));
            host.appendChild(
                field(
                    "Type",
                    select(
                        Object.keys(MARKER_ICON).map((kind) => ({ value: kind, label: titleCase(kind) })),
                        marker.kind,
                        (v) => {
                            marker.kind = v as MarkerKind;
                            markDirty();
                        },
                    ),
                ),
            );
            if (CONNECTOR_KINDS.has(marker.kind)) renderConnectorControls(host, marker);
        }

        if (selection.kind === "opening") {
            const opening = selection.opening;
            const heading = document.createElement("h3");
            heading.textContent = "Opening";
            host.appendChild(heading);
            host.appendChild(
                field(
                    "Type",
                    select(OPENING_KINDS, opening.kind, (v) => {
                        opening.kind = v as Opening["kind"];
                        markDirty();
                    }),
                ),
            );
        }

        // Not for a room: a room seed is a name attached to a region, so
        // deleting it removes the name and leaves every wall standing, which
        // reads as a delete that did not work. Removing a room for real is
        // renderRoomDeleteControl's job, and it says what it will take.
        // Appended once for whatever is selected, rather than inside each of
        // the per-kind branches above: these fields come from a base class that
        // every item shares, so a form that had to remember to include them
        // would eventually forget for one kind.
        const details: ItemDetails | null =
            selection.kind === "wall"
                ? selection.wall
                : selection.kind === "opening"
                  ? selection.opening
                  : selection.kind === "room"
                    ? selection.room
                    : selection.kind === "marker"
                      ? selection.marker
                      : null;
        if (details) renderItemDetails(host, details, itemKey(selection));

        if (selection.kind === "room") renderRoomDeleteControl(host, selection.room);
        else {
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "btn btn--sm btn--danger";
            remove.textContent = "Delete";
            remove.addEventListener("click", deleteSelection);
            host.appendChild(remove);
        }
    }

    /**
     * Link a stair or lift to its counterpart on an adjacent floor.
     *
     * Two markers sharing a ``connector_id`` are the same physical shaft. The
     * id is authored here rather than derived from position because a
     * switchback stair genuinely lands somewhere else on the floor above, so
     * proximity would be wrong exactly when it mattered.
     */
    /**
     * A room's boundary walls, split three ways.
     *
     * ``unique`` is a wall this room alone relies on: it bounds no other face
     * *and* is not part of the building's shell. Those travel with the room.
     * ``shared`` is everything else on the boundary, which stretches to keep
     * up rather than moving wholesale.
     *
     * The exterior exclusion is load-bearing and easy to talk yourself out of.
     * Topologically an exterior wall usually does bound exactly one room - in
     * a shell split by a single partition, the west wall bounds only the west
     * room - so a purely topological "unique" hands that wall to the room and
     * dragging the room tears the side off the building.
     *
     * A room with no unique walls at all is therefore one whose every side is
     * shell or shared. There is nothing for a move or a delete to act on, and
     * both callers check for it rather than running a gesture that does
     * nothing.
     *
     * Returns null for an unbound seed (no face, so no boundary to gather).
     */
    function roomBoundaryWalls(room: RoomSeed): { face: Face; unique: Wall[]; shared: Wall[] } | null {
        const face = faceForSeed({ x: room.x, y: room.y }, state.faces);
        if (!face) return null;
        const boundary = floor().walls.filter((wall) => face.wallIds.includes(wallId(wall)));
        const unique = boundary.filter((wall) => wall.kind !== "exterior" && !state.faces.some((other) => other !== face && other.wallIds.includes(wallId(wall))));
        const shared = boundary.filter((wall) => !unique.includes(wall));
        return { face, unique, shared };
    }

    /**
     * Offer to delete a whole room at once - its seed and the walls that are
     * only ever this room's - rather than one wall at a time.
     *
     * Nothing is offered when the room has no walls of its own to lose (it
     * sits inside the shell, or every side is shared with a neighbour). The
     * room is still a room; there is simply no destructive action that would
     * mean anything, and a button that only cleared its name was read as a
     * delete that had failed.
     */
    function renderRoomDeleteControl(host: HTMLElement, room: RoomSeed): void {
        const boundary = roomBoundaryWalls(room);
        if (!boundary) return;
        const walls = boundary.unique;
        if (!walls.length) return;
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn btn--sm btn--danger";
        button.textContent = `Delete room and its ${walls.length} wall${walls.length === 1 ? "" : "s"}`;
        button.addEventListener("click", () => {
            state.multi = [{ kind: "room", room }, ...walls.map((wall): SelectionItem => ({ kind: "wall", wall }))];
            state.selection = state.multi[0] as SelectionItem;
            deleteSelection();
        });
        host.appendChild(button);
    }

    function renderConnectorControls(host: HTMLElement, marker: Marker): void {
        const current = floor();
        const candidates: Array<{ floor: Floor; marker: Marker }> = [];
        for (const other of state.doc.floors) {
            if (Math.abs(other.level - current.level) !== 1) continue;
            for (const candidate of other.markers) {
                if (CONNECTOR_KINDS.has(candidate.kind)) candidates.push({ floor: other, marker: candidate });
            }
        }

        const wrap = document.createElement("div");
        wrap.className = "floorplan-connector";
        const title = document.createElement("span");
        title.className = "floorplan-field__label";
        title.textContent = "Connects floors";
        wrap.appendChild(title);

        if (marker.connector_id) {
            const linked = document.createElement("p");
            linked.className = "floorplan-hint";
            const others = state.doc.floors
                .filter((item) => item !== current)
                .filter((item) => item.markers.some((candidate) => candidate.connector_id === marker.connector_id))
                .map((item) => item.name || floorLabels().get(item) || `Level ${item.level}`);
            linked.textContent = others.length ? `Linked to ${others.join(", ")}.` : "Linked, but nothing on another floor shares this connector yet.";
            wrap.appendChild(linked);
            const unlink = document.createElement("button");
            unlink.type = "button";
            unlink.className = "btn btn--sm btn--ghost";
            unlink.textContent = "Unlink";
            unlink.addEventListener("click", () => {
                marker.connector_id = null;
                renderSidebar();
                markDirty();
            });
            wrap.appendChild(unlink);
        } else if (!candidates.length) {
            const none = document.createElement("p");
            none.className = "floorplan-hint";
            none.textContent = "Add a stair or lift on the floor above or below to link them.";
            wrap.appendChild(none);
        } else {
            for (const candidate of candidates) {
                const button = document.createElement("button");
                button.type = "button";
                button.className = "btn btn--sm btn--ghost";
                const where = candidate.floor.name || floorLabels().get(candidate.floor) || `Level ${candidate.floor.level}`;
                button.textContent = `Link to ${candidate.marker.name || candidate.marker.kind} on ${where}`;
                button.addEventListener("click", () => {
                    // Adopt the counterpart's id when it already has one, so a
                    // third floor joins the same shaft rather than starting a
                    // parallel one.
                    const shared = candidate.marker.connector_id || nextLocalId();
                    candidate.marker.connector_id = shared;
                    marker.connector_id = shared;
                    renderSidebar();
                    markDirty();
                });
                wrap.appendChild(button);
            }
        }
        host.appendChild(wrap);
    }

    /**
     * Square the drawing axis to a wall the user picks.
     *
     * Angle snapping works in 45-degree steps around this axis. Buildings sit
     * at arbitrary angles to true north, so without this "right angle" means
     * right-angle-to-the-equator and fights every wall on screen.
     */
    function renderAxisControl(host: HTMLElement, wall: Wall): void {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn btn--sm btn--ghost";
        button.textContent = "Square the grid to this wall";
        button.addEventListener("click", () => {
            const radians = Math.atan2(wall.by - wall.ay, wall.bx - wall.ax);
            // Fold into [0, 90) - a wall and its perpendicular describe the
            // same grid, so the axis only ever needs a quarter turn.
            const degrees = ((radians * 180) / Math.PI + 360) % 90;
            state.doc.rotation_degrees = degrees;
            markDirty();
            toast.info("Right angles now follow this wall.");
        });
        host.appendChild(button);
    }

    function updateEmptyState(current: Floor): void {
        const empty = document.getElementById("floorplan-empty");
        if (!empty) return;
        if (state.loadFailed) {
            empty.replaceChildren(el("h2", "Could not load this floorplan."), el("p", "Nothing here has been saved. Reload the page to try again."));
            empty.hidden = false;
            return;
        }
        empty.hidden = current.walls.length > 0;
    }

    /** A text element, for the handful of places that build one inline. */
    function el(tag: string, text: string): HTMLElement {
        const node = document.createElement(tag);
        node.textContent = text;
        return node;
    }

    // ------------------------------------------------------------ persistence

    /** Whether this document is something other than the viewer's own saved
     * work - a wiki-published plan, or (once REData floorplans exist)
     * upstream data - so editing it should say so before anyone draws over
     * it. Saving always forks the viewer's own local copy either way. */
    function renderOriginBanner(): void {
        const banner = document.getElementById("floorplan-origin-banner");
        if (!banner) return;
        if (state.doc.origin === "community") {
            banner.textContent = "Published to this place's community wiki by another explorer. Saving creates your own version.";
            banner.hidden = false;
        } else if (state.doc.origin === "redata") {
            banner.textContent = "From REData. Saving creates your own version.";
            banner.hidden = false;
        } else {
            banner.hidden = true;
        }
    }

    /** Every other version of this plan the viewer has, so "Save as new
     * version" (and publishing an older baseline) leads somewhere instead of
     * forking a plan nothing ever lets the user find again. */
    function renderVersions(): void {
        const container = document.getElementById("floorplan-versions");
        if (!container) return;
        const versions = state.versions || [];
        container.innerHTML = "";
        container.hidden = versions.length < 2;
        if (versions.length < 2) return;
        const label = document.createElement("span");
        label.className = "floorplan-field__label";
        label.textContent = "Versions";
        container.appendChild(label);
        for (const version of versions) {
            const isCurrent = version.uuid === state.doc.uuid;
            const button = document.createElement("button");
            button.type = "button";
            button.className = "btn btn--ghost";
            button.textContent = isCurrent ? `${version.name || "Untitled"} (current)` : version.name || "Untitled";
            button.title = version.valid_from ? `In force from ${version.valid_from}` : "The original baseline";
            button.disabled = isCurrent;
            button.addEventListener("click", () => void switchVersion(version.uuid));
            container.appendChild(button);
        }
    }

    /** Adopt a just-fetched document (initial load or a version switch) and
     * refresh everything that depends on it. */
    function finishLoadingDocument(): void {
        const anchor = state.doc.plan_origin || { lat, lng };
        state.doc.plan_origin = anchor;
        projection = new PlanProjection(anchor);
        // Restores a saved plan already turned to face its building -
        // fitToContent() below then fits bounds against the rotated view,
        // not the unrotated one.
        map.setBearing(state.doc.rotation_degrees || 0);
        const nameInput = document.getElementById("floorplan-name") as HTMLInputElement | null;
        if (nameInput) nameInput.value = state.doc.name || "";
        const validFrom = document.getElementById("floorplan-valid-from") as HTMLInputElement | null;
        if (validFrom) validFrom.value = state.doc.valid_from || "";
        // A brand-new plan starts from the real footprint when one is known, so
        // the first thing on screen is the building rather than a blank map.
        const fresh = !state.loadFailed && state.doc.floors.length === 1 && !(state.doc.floors[0] as Floor).walls.length;
        if (fresh && seedFromOutline(state.doc.floors[0] as Floor)) state.dirty = true;
        else state.dirty = false;
        // A switch away from the version that was selected/mid-drag leaves
        // stale references into a document that no longer exists.
        // Sparse or colliding levels arrive from a mid-stack delete by an older
        // client, and from third-party imports. Repaired without marking the
        // document dirty: the next real edit persists it.
        normaliseFloors(state.doc.floors[0] as Floor | undefined || null);
        clearSelection();
        // An undo snapshot outliving the document it was taken from is not a
        // safety net: applying it writes the *previous* version's contents,
        // carrying that version's uuid, over the one now open.
        clearHistory();
        state.floorIndex = 0;
        setTool("select");
        renderOriginBanner();
        renderVersions();
        render();
        fitToContent();
        updateMoreMenu();
        // A footprint-seeded plan is dirty the instant it loads (see above) -
        // with no Save button any more, nothing else would ever persist it.
        if (state.dirty && !state.loadFailed) queueAutosave();
        else updateSaveStatus();
    }

    async function load(): Promise<void> {
        try {
            const response = await fetch(jsonUrl, { headers: { Accept: "application/json" } });
            if (response.status === 204) {
                state.doc = emptyDocument({ lat, lng });
            } else if (response.ok) {
                const body = (await response.json()) as FloorplanDocument;
                state.doc = { ...emptyDocument({ lat, lng }), ...body };
                if (!state.doc.floors?.length) state.doc.floors = emptyDocument({ lat, lng }).floors;
                state.versions = body.versions || [];
            } else {
                // Neither branch above matched, so state.doc is still the
                // blank document state was initialised with. Left unflagged,
                // the seeding and autosave at the end of
                // finishLoadingDocument() would persist that blank as a new
                // version - which then wins the most-recent tie-break and
                // reads as though the real plan had been deleted.
                state.loadFailed = true;
                toast.error("Could not load this floorplan. Reload to try again.");
            }
        } catch {
            state.loadFailed = true;
            toast.error("Could not load this floorplan. Reload to try again.");
        }
        finishLoadingDocument();
    }

    /** Switch to another saved version, the way "Save as new version" implies
     * one can be switched back to - see renderVersions(). */
    async function switchVersion(uuid: string): Promise<void> {
        // Nothing prompts before switching - autosave already means the user
        // never explicitly asked to "save", so flush whatever is pending
        // instead of discarding it the way abandoning the page might.
        if (state.dirty) await save(false);
        // A save for the version being left could still be in flight; letting
        // its response land after the switch would overwrite state.doc.uuid
        // (by then the *new* version's) back to the one just left.
        await waitForSaveSlot();
        try {
            const response = await fetch(`${jsonUrl}?version=${encodeURIComponent(uuid)}`, { headers: { Accept: "application/json" } });
            if (!response.ok) {
                toast.warning("Could not load that version.");
                return;
            }
            const body = (await response.json()) as FloorplanDocument;
            state.doc = { ...emptyDocument({ lat, lng }), ...body };
            if (!state.doc.floors?.length) state.doc.floors = emptyDocument({ lat, lng }).floors;
            state.versions = body.versions || [];
        } catch {
            toast.warning("Could not load that version.");
            return;
        }
        finishLoadingDocument();
    }

    /** A frozen-order, same-object record of what a save's payload actually
     * held, taken at the moment it was sent - see snapshotForSend(). */
    interface SentSnapshot {
        floor: Floor;
        walls: Wall[];
        wallOpenings: Opening[][];
        rooms: RoomSeed[];
        markers: Marker[];
    }

    /**
     * Record each floor's item arrays *in the order sent*, without copying
     * the items themselves - editing (including deleting something) can
     * continue on `state.doc` while this save's request is in flight, and
     * applyServerIds() must still land each returned uuid on the exact
     * object that was actually sent, not on whatever a live array index
     * happens to point at once the response arrives.
     */
    function snapshotForSend(doc: FloorplanDocument): SentSnapshot[] {
        return doc.floors.map((floor) => ({
            floor,
            walls: [...floor.walls],
            wallOpenings: floor.walls.map((wall) => [...wall.openings]),
            rooms: [...floor.rooms],
            markers: [...floor.markers],
        }));
    }

    /**
     * Copy the server's real per-item uuids back onto the objects a save
     * actually sent, matched positionally against `sent` rather than by uuid
     * (the client's own new items don't have a real one yet) and rather than
     * by live array position (see snapshotForSend()). This is safe because
     * `_sync` on the server assigns `sort_order` from the payload's array
     * index and every item's `Meta.ordering` starts with `sort_order`, so
     * `saved.floors[i]` is exactly the row that came from `sent[i]` in the
     * same request - whatever was created keeps its position, and only
     * orphaned items (already absent from the payload) are missing from
     * `saved`.
     */
    function applyServerIds(sent: SentSnapshot[], saved: FloorplanDocument): void {
        (saved.floors || []).forEach((savedFloor, floorIndex) => {
            const entry = sent[floorIndex];
            if (!entry) return;
            entry.floor.uuid = savedFloor.uuid;
            (savedFloor.walls || []).forEach((savedWall, wallIndex) => {
                const wall = entry.walls[wallIndex];
                if (!wall) return;
                wall.uuid = savedWall.uuid;
                (savedWall.openings || []).forEach((savedOpening, openingIndex) => {
                    const opening = entry.wallOpenings[wallIndex]?.[openingIndex];
                    if (opening) opening.uuid = savedOpening.uuid;
                });
            });
            (savedFloor.rooms || []).forEach((savedRoom, roomIndex) => {
                const room = entry.rooms[roomIndex];
                if (room) room.uuid = savedRoom.uuid;
            });
            (savedFloor.markers || []).forEach((savedMarker, markerIndex) => {
                const marker = entry.markers[markerIndex];
                if (marker) marker.uuid = savedMarker.uuid;
            });
        });
    }

    /**
     * Persist the current document.
     *
     * Called automatically a little after each edit (see queueAutosave()),
     * and directly for the two deliberate actions in the "more" menu -
     * asNewVersion forks a dated version instead of overwriting this one,
     * which is not something autosave should ever do on its own.
     */
    async function save(asNewVersion = false): Promise<void> {
        // Two overlapping saves would each overwrite state.doc.uuid from
        // their own response when they resolve, regardless of which request
        // was sent first - whichever *resolves* last wins, which can silently
        // revert a just-forked "new version" back to the one it forked from.
        await waitForSaveSlot();
        const nameInput = document.getElementById("floorplan-name") as HTMLInputElement | null;
        const validFrom = document.getElementById("floorplan-valid-from") as HTMLInputElement | null;
        // A plan name is rarely worth asking for up front - the floor it is
        // on already has one ("Ground floor"), and that is a fine default a
        // user can override in "Add more details" when it matters.
        state.doc.name = nameInput?.value || floor().name || "";
        state.doc.valid_from = validFrom?.value || null;
        // Every marker's WGS-84 position, freshly computed here rather than
        // kept live at each edit site (placement, drag) - x/y is the single
        // source of truth, and this is the one place that has to convert it
        // for the server, which needs real coordinates to place this
        // marker's detail-pin twin (see services.floorplans.serialization).
        for (const item of state.doc.floors) {
            for (const marker of item.markers) {
                const world = toLatLng({ x: marker.x, y: marker.y });
                marker.lat = world[0];
                marker.lng = world[1];
            }
        }
        const payload: FloorplanDocument = { ...state.doc };
        // Dropping the uuid is what makes the save fork a new dated version
        // instead of overwriting the one that was loaded. A document that
        // arrived from somewhere other than this user's own plans always
        // forks: the banner promises "saving creates your own version", and
        // an autosave firing a second after the page opened is nobody's
        // decision to edit someone else's published work in place.
        if (asNewVersion || state.doc.origin !== "local") delete payload.uuid;
        delete payload.origin;
        delete payload.versions;
        // Taken now, before anything below can await - state.doc may keep
        // changing while this request is in flight (see snapshotForSend()).
        const sent = snapshotForSend(state.doc);

        saving = true;
        updateSaveStatus();
        try {
            const response = await fetch(saveUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                body: JSON.stringify(payload),
            });
            if (!response.ok) {
                // The save view always answers errors as {ok, error} JSON -
                // anything else (an nginx/proxy error page for a 502, a bare
                // 500 with no body of its own) is not a message meant for a
                // person, and toasting it raw once dumped a whole HTML error
                // page - headings and all - into the toast.
                let message = `Could not save this floorplan. (${response.status})`;
                try {
                    const body = (await response.json()) as { error?: string };
                    if (body.error) message = body.error;
                } catch {
                    // Not JSON - keep the generic, status-coded message above.
                }
                toast.warning(message);
                noteSaveFailure();
                return;
            }
            // The save view answers {ok, floorplan: <document>} - the uuid is
            // nested, and picking it up is what makes the next save update
            // this version instead of forking another one. Every nested
            // item's own real uuid is in there too and has to come back the
            // same way: the server matches an item to an existing row purely
            // by uuid, so anything still carrying its client-only local id
            // (every item created this session) would otherwise be unmatched
            // on the *next* save, silently deleted as an "orphan" and
            // recreated under a new identity - see applyServerIds().
            const body = (await response.json()) as { ok?: boolean; floorplan?: FloorplanDocument };
            if (body.floorplan) {
                state.doc.uuid = body.floorplan.uuid;
                applyServerIds(sent, body.floorplan);
                // The response reports where the saved row actually lives, so
                // a document loaded as someone else's community plan becomes
                // "local" here precisely because the save forked it.
                state.doc.origin = body.floorplan.origin;
                state.versions = body.floorplan.versions || [];
            }
            state.dirty = false;
            saveFailed = false;
            retryAttempt = 0;
            updateMoreMenu();
            renderOriginBanner();
            renderVersions();
            // Autosave stays quiet - a toast for every keystroke-driven save
            // would be constant noise. The two explicit "more" menu actions
            // still confirm themselves; nothing else calls this with asNewVersion.
            if (asNewVersion) toast.success("Saved as a new version.");
        } catch {
            toast.warning("Could not save this floorplan.");
            noteSaveFailure();
        } finally {
            saving = false;
            updateSaveStatus();
        }
    }

    /** Arm the next retry after a failed save, backing off as they pile up. */
    function noteSaveFailure(): void {
        saveFailed = true;
        const delay = RETRY_DELAYS[Math.min(retryAttempt, RETRY_DELAYS.length - 1)] as number;
        retryAttempt += 1;
        // Through queueAutosave so a retry cannot overlap an in-flight save.
        queueAutosave(delay);
    }

    /**
     * "Save as new version" and "Publish to wiki" only make sense once
     * there is a first version to fork or publish - shown to a brand-new
     * plan, both would either mean the same thing as Save or fail outright.
     */
    function updateMoreMenu(): void {
        const hasVersion = Boolean(state.doc.uuid);
        for (const id of ["floorplan-save-version", "floorplan-publish"]) {
            const button = document.getElementById(id) as HTMLButtonElement | null;
            if (button) button.disabled = !hasVersion;
        }
    }

    const moreToggle = document.getElementById("floorplan-more-toggle") as HTMLButtonElement | null;
    const moreList = document.getElementById("floorplan-more-list");
    function closeMoreMenu(): void {
        if (!moreList || moreList.hidden) return;
        moreList.hidden = true;
        moreToggle?.setAttribute("aria-expanded", "false");
    }
    moreToggle?.addEventListener("click", (event) => {
        event.stopPropagation();
        if (!moreList) return;
        moreList.hidden = !moreList.hidden;
        moreToggle.setAttribute("aria-expanded", String(!moreList.hidden));
    });
    document.addEventListener("click", (event) => {
        if (moreList && !moreList.hidden && !moreList.contains(event.target as Node) && event.target !== moreToggle) closeMoreMenu();
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") closeMoreMenu();
    });

    document.getElementById("floorplan-save-version")?.addEventListener("click", () => {
        closeMoreMenu();
        void save(true);
    });
    document.getElementById("floorplan-publish")?.addEventListener("click", () => {
        closeMoreMenu();
        void (async () => {
            if (!state.doc.uuid) {
                toast.info("Save the floorplan before publishing it.");
                return;
            }
            if (state.dirty && !window.confirm("Publish the last saved version? Unsaved changes are not included.")) return;
            // Shares the same in-flight flag as save() - both so this can't
            // overlap an autosave (the same race a concurrent save() call
            // could hit), and so it gets the same "Saving..." feedback for
            // free instead of leaving the button looking inert during its
            // own round trip.
            await waitForSaveSlot();
            saving = true;
            updateSaveStatus();
            try {
                const response = await fetch(publishUrl, {
                    method: "POST",
                    headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                    body: JSON.stringify({ uuid: state.doc.uuid }),
                });
                const body = await response.json().catch(() => ({}) as { ok?: boolean; error?: string });
                if (!response.ok || !body.ok) {
                    toast.warning(body.error || "Could not publish this floorplan.");
                    return;
                }
                toast.success("Published to the community wiki.");
            } finally {
                saving = false;
                updateSaveStatus();
            }
        })();
    });

    for (const button of document.querySelectorAll<HTMLButtonElement>("[data-tool]")) {
        button.addEventListener("click", () => setTool((button.dataset.tool as Tool) || "select"));
    }
    // Kept working for anything that still renders one; the toolbar's own
    // marker-kind buttons now live in the tool options panel.
    for (const button of document.querySelectorAll<HTMLButtonElement>("[data-marker-kind]")) {
        button.addEventListener("click", () => {
            state.markerKind = button.dataset.markerKind as MarkerKind;
            setTool("marker");
        });
    }
    document.getElementById("floorplan-start-outline")?.addEventListener("click", () => {
        checkpoint();
        if (seedFromOutline(floor())) markDirty();
        else toast.info("No building outline is known for this place yet.");
    });
    document.getElementById("floorplan-start-rectangle")?.addEventListener("click", () => {
        // Four walls around the current view's middle third: the fastest way to
        // show that closing a loop produces a room.
        checkpoint();
        const bounds = map.getBounds();
        const a = toLocal(bounds.getSouthWest());
        const b = toLocal(bounds.getNorthEast());
        const insetX = (b.x - a.x) / 3;
        const insetY = (b.y - a.y) / 3;
        const corners: Pt[] = [
            { x: a.x + insetX, y: a.y + insetY },
            { x: b.x - insetX, y: a.y + insetY },
            { x: b.x - insetX, y: b.y - insetY },
            { x: a.x + insetX, y: b.y - insetY },
        ];
        for (let i = 0; i < corners.length; i++) {
            const p = corners[i] as Pt;
            const q = corners[(i + 1) % corners.length] as Pt;
            floor().walls.push({ uuid: nextLocalId(), kind: "exterior", thickness: "normal", ax: p.x, ay: p.y, bx: q.x, by: q.y, openings: [] });
        }
        markDirty();
    });

    window.addEventListener("beforeunload", (event) => {
        if (state.dirty) event.preventDefault();
    });

    void load();
}

function titleCase(s: string): string {
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

/** Escapes a user-provided string (a room/plan name) for interpolation into
 * a Leaflet tooltip's HTML content, which renders its string argument as
 * markup rather than plain text. */
function escHtml(value: string): string {
    return value.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string);
}

function readJson<T>(id: string): T | null {
    const node = document.getElementById(id);
    if (!node) return null;
    try {
        return JSON.parse(node.textContent || "null") as T;
    } catch {
        return null;
    }
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();
