/**
 * Floorplan editor: draw walls, get rooms.
 */

import { safeColor } from "../shared/markup-engine";
import { getCsrfToken } from "../shared/csrf";
import { toast } from "../shared/dialogs";
import { isTypingTarget } from "../shared/hotkeys";
import { installUndoBar, registerLocalUndoProvider, syncUndoBar } from "../shared/undo-bar";
import { PlanProjection, type Pt, distance, interiorPoint, pointInRing, projectOnSegment, rotate } from "../shared/floorplan/coords";
import {
    type Floor,
    type Lock,
    type Opening,
    type FloorplanDocument,
    type Marker,
    type MarkerKind,
    type Reference,
    type RoomSeed,
    type VersionSummary,
    type Wall,
    emptyDocument,
    type ItemDetails,
    LOCK_STATES,
    OPENING_KINDS,
    WALL_KINDS,
    attribute,
    copyFloorContents,
    newConnectorId,
    nextLocalId,
    setAttribute,
    wallEnd,
    wallId,
    wallLength,
    wallStart,
    wallSegments,
} from "../shared/floorplan/document";
import { installGlobalColorPicker } from "../shared/color-picker";
import { CONNECTOR_KINDS, connectorCandidates } from "../shared/floorplan/connectors";
import { OPENING_SWINGS, doorLeaves, rehostOpening, swings } from "../shared/floorplan/openings";
import { type RoomBoundary, splitRoomBoundary } from "../shared/floorplan/rooms";
import { applyServerIds, snapshotForSend } from "../shared/floorplan/sync";
import { GROUND_LABEL, contiguousLevels, deriveDesignations } from "../shared/floorplan/designations";
import { type DragModifiers, DragGesture, constrainToAxis, modifiersOf, snapRotation } from "../shared/floorplan/drag";
import { installGlobalIconPicker } from "../shared/icon-picker";
import { History } from "../shared/floorplan/history";
import { type Face, deriveFaces, faceForSeed } from "../shared/floorplan/planar";
import { GRID_SPACING_METERS, PIXEL_TOLERANCES, clampOpening, snapPoint, snapTranslation } from "../shared/floorplan/snapping";
import { createMapImageOverlays, wireManageOverlaysDialog, type MapOverlayEntry } from "../shared/map-image-overlays";
import { createMapLayers } from "../shared/map-layers";

declare const L: typeof import("leaflet");

// @types/leaflet has no idea leaflet-rotate (loaded after leaflet.js in editor.html) exists.
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

const WALL_STYLE: Record<string, { color: string; weight: number; dashArray?: string }> = {
    // Same blue as a building's boundary on the Private Pin page - it plays the same role here.
    exterior: { color: "#1d4ed8", weight: 5 },
    interior: { color: "#546e7a", weight: 3 },
    // Finely dotted and warmer than the greys: a boundary, drawn as something other than the building.
    fence: { color: "#8d6e63", weight: 2, dashArray: "1 4" },
    virtual: { color: "#90a4ae", weight: 2, dashArray: "6 6" },
    collapsed: { color: "#a1887f", weight: 3, dashArray: "2 6" },
};

/**
 * The key that arms the marker tool with each kind already chosen.
 */
const MARKER_KEYS: Record<MarkerKind, string> = { hazard: "h", stair: "s", elevator: "e" };

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

// A white base once the exterior is drawn, so the plan reads as a real floor rather than a translucent overlay on the map beneath it.
const ROOM_FILL = { color: "#00897b", weight: 1, fillColor: "#eef6f4", fillOpacity: 0.94 };
const UNBOUND_FILL = { color: "#b0bec5", weight: 1, dashArray: "4 4", fillColor: "#ffffff", fillOpacity: 0.92 };

/**
 * A marker icon, styled like the site's detail-pin markers elsewhere (map-annotations.ts's `detailIcon()`).
 */
// A marker's icon/color prefer its linked detail pin's own customizations.
function markerIcon(marker: Marker, selected: boolean): L.DivIcon {
    // Validated here as well as on write: this is interpolated into a divIcon's `html`, and a stored value predating the column's own.
    const color = safeColor(marker.color, MARKER_COLOR[marker.kind] || "#2563eb");
    const glyph = marker.icon || MARKER_ICON[marker.kind] || "place";
    const size = 22;
    const pad = 5;
    const total = size + pad * 2;
    const ring = selected ? "outline:3px solid #f57c00;outline-offset:2px;" : "";
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
    // Only when the name says something the kind doesn't already.
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

/**
 * Reveals the "map didn't load" state and stands the editor down.
 */
function showMapUnavailable(): void {
    // Otherwise the "Draw the walls" prompt, which the server renders visible and
    // the editor normally hides, sits underneath this one.
    const prompt = document.getElementById("floorplan-empty");
    if (prompt) prompt.hidden = true;
    const notice = document.getElementById("floorplan-unavailable");
    if (notice) notice.hidden = false;
    document.getElementById("floorplan-retry-load")?.addEventListener("click", () => window.location.reload());
}

function boot(): void {
    // Leaflet and leaflet-rotate are CDN scripts, so they are absent whenever that request does not land.
    if (typeof L === "undefined") {
        showMapUnavailable();
        return;
    }
    // The shared picker's markup calls IconPicker.* from inline onclick, so the
    // global has to exist before any of it is clicked.
    installGlobalIconPicker();
    // Both pickers are server-rendered markup calling a window global from an
    // inline onclick, so the page that uses them has to install them.
    installGlobalColorPicker();
    installUndoBar();
    const mapElement = document.getElementById("floorplan-map");
    if (!mapElement) return;
    // Rebound so the null check survives into the closures below.
    const mapEl: HTMLElement = mapElement;

    const jsonUrl = mapEl.dataset.jsonUrl || "";
    const saveUrl = mapEl.dataset.saveUrl || "";
    const publishUrl = mapEl.dataset.publishUrl || "";
    const lat = parseFloat(mapEl.dataset.lat || "0");
    const lng = parseFloat(mapEl.dataset.lng || "0");

    // attributionControl: false - required attribution renders in the page footer instead.
    const canRotateView = typeof (L.Map.prototype as { setBearing?: unknown }).setBearing === "function";
    // A tool that cannot do anything should not be on the toolbar offering to.
    if (!canRotateView) (document.querySelector('[data-tool="rotate"]') as HTMLElement | null)?.remove();

    // rotate/touchRotate/shiftKeyRotate/rotateControl (leaflet-rotate, loaded in editor.html).
    const map = L.map("floorplan-map", {
        // Leaflet's own default top-left zoom control, left exactly as every other map on the site renders it (same corner, same classes).
        doubleClickZoom: false,
        attributionControl: false,
        boxZoom: false,
        rotate: canRotateView,
        touchRotate: canRotateView,
        shiftKeyRotate: canRotateView,
        rotateControl: canRotateView && { position: "topright", closeOnZeroBearing: false },
    }).setView([lat, lng], 20);

    // leaflet-rotate's own control is removed rather than re-homed.
    map.getContainer().querySelector(".leaflet-control-rotate")?.remove();

    // Declared before createMapLayers below: its "underlay" custom toggle reads state.showUnderlay synchronously while the panel builds its.
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
        /**
 * What the wall tool draws next.
 */
        wallKind: "exterior" as Wall["kind"],
        /** What the opening tool cuts next - this is where windows live. */
        openingKind: "door" as Opening["kind"],
        /** Snapping, as a setting. The backtick key suspends it momentarily. */
        snapEnabled: true,
        /** Whether the connector picker is showing every floor or just the near ones. */
        connectorsExpanded: false,
        dirty: false,
        suspendSnap: false,
        faces: [] as Face[],
        versions: [] as VersionSummary[],
        showUnderlay: false,
        showGrid: false,
        /** The plan could not be fetched, so what is on screen is not it. */
        loadFailed: false,
        /**
         * Another tab has saved over the version this one is editing. Saving
         * again would delete their work, so nothing here saves any more.
         */
        superseded: false,
    };

    /**
 * The backdrop to trace over, via the same shared layers engine and panel every other map on the site uses.
 */
    // Created for its side effect - it registers the basemap and overlay
    // layers on the map - and nothing here holds on to the result.
    createMapLayers(map, {
        root: document.getElementById("floorplan-layers"),
        defaultBase: "satellite",
        contextMenu: false, // this editor has its own specialised right-click menu
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
            grid: {
                isActive: () => state.showGrid,
                toggle: () => {
                    state.showGrid = !state.showGrid;
                    renderGrid();
                },
            },
        },
    });

    const outline = readJson<Array<[number, number]>>("floorplan-outline") || [];
    /**
 * This pin's own photos, offered for attaching to walls, doors and locks.
 */
    const pinPhotos = readJson<Array<{ uuid: string; url: string; caption: string }>>("floorplan-photos") || [];
    const overlayCornersTemplate = mapEl.dataset.overlayCornersUrlTemplate || "";
    // Always created, even with zero overlays at load time.
    const overlayControl = createMapImageOverlays(L, map, {
        cornersUrl: (uuid) => overlayCornersTemplate.replace("00000000-0000-0000-0000-000000000000", uuid),
        csrfToken: getCsrfToken(),
        onError: (message) => toast.error(message),
    });
    overlayControl.sync(readJson<MapOverlayEntry[]>("floorplan-overlays") || []);
    document.body.addEventListener("ul:map-overlays-changed", (e) => {
        overlayControl.sync((e as CustomEvent).detail?.overlays || []);
    });
    wireManageOverlaysDialog({
        map,
        control: overlayControl,
        onAlignStart: () => (document.getElementById("map-overlays-dialog") as HTMLDialogElement | null)?.close(),
    });
    const alignOnLoad = new URLSearchParams(window.location.search).get("align");
    if (alignOnLoad) {
        overlayControl.setVisible(alignOnLoad, true);
        window.ulMapOverlayStartAlign?.(alignOnLoad);
    }

    let projection = new PlanProjection({ lat, lng });
    // First, so it always paints beneath everything else.
    const gridLayer = L.layerGroup().addTo(map);
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

    /** The grid-snap option for snapPoint(), or null while the grid is off. */
    function gridOption(): { spacing: number; tolerance: number } | null {
        return state.showGrid ? { spacing: GRID_SPACING_METERS, tolerance: tolerances().endpoint } : null;
    }

    /**
 * How many drags are in flight.
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
            // Nothing is done to this press until it has travelled far enough to be a drag.
            activeDrags += 1;

            // The press identifies *what* was grabbed, but the rest of the gesture is tracked on the map container, which outlives it. render() runs.
            const surface = map.getContainer();
            const gesture = new DragGesture({ x: event.clientX, y: event.clientY }, modifiersOf(event), handlers.slopPx);
            let moved = false;
            let finished = false;

            const onMove = (rawMove: Event): void => {
                const moveEvent = rawMove as PointerEvent;
                if (moveEvent.pointerId !== event.pointerId) return;
                if (!gesture.advance({ x: moveEvent.clientX, y: moveEvent.clientY })) return;
                if (!moved) {
                    moved = true;
                    // Everything that interferes waits for this moment, when
                    // the gesture is certainly a drag and not a click.
                    map.dragging.disable();
                    try {
                        // Capture retargets pointerup to whatever holds it, and the browser fires click at the common ancestor of the press and the release.
                        surface.setPointerCapture(event.pointerId);
                    } catch {
                        // Best effort: it keeps a pointer that wanders off the
                        // map reporting here. The window listeners work without it.
                    }
                }
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
                if (moved) {
                    map.dragging.enable();
                    // The release of a real drag still reads as a click on whatever lies underneath, which would otherwise re-select or deselect the thing.
                    suppressNextClick = true;
                }
                // Queued before the handler runs, not after.
                if (moved) renderSoon();
                handlers.end?.(moved);
            };
            // On window rather than the map: a pointer released outside the map still has to end the gesture, or the listeners stay and panning.
            window.addEventListener("pointermove", onMove);
            window.addEventListener("pointerup", onFinish);
            window.addEventListener("pointercancel", onFinish);
            window.addEventListener("lostpointercapture", onFinish);
        });
    }

    /**
 * Snap a rigid translation against this floor, minus what the drag itself rewrites.
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
 * Backoff for retries, in milliseconds.
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
        // The single funnel every edit reaches, so one guard here is enough.
        if (state.loadFailed || state.superseded) return;
        updateSaveStatus();
        if (autosaveTimer !== null) clearTimeout(autosaveTimer);
        const attempt = (): void => {
            // A save from a moment ago is still in flight (slow network, fast edits).
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
        if (retry) retry.hidden = !saveFailed || saving || state.superseded;
        if (state.superseded) {
            el.textContent = "Changed elsewhere";
            el.className = "floorplan-save-status is-error";
            // Told to reload and given nothing to press is a dead end, and the
            // one thing that resolves this is exactly one action.
            const reload = document.getElementById("floorplan-reload");
            if (reload) reload.hidden = false;
            return;
        }
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
 * Record the document as it stands, so the edit about to happen becomes one undo step.
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

    /**
 * Show a delete control on the canvas whenever something is selected.
 */
    function updateDeleteButton(): void {
        const button = document.getElementById("floorplan-delete") as HTMLButtonElement | null;
        if (!button) return;
        const count = state.multi.length;
        button.hidden = count === 0;
        button.setAttribute("aria-label", count > 1 ? `Delete ${count} items` : "Delete selection");
    }

    function updateHistoryButtons(): void {
        syncUndoBar();
    }

    /**
 * Disable tools that have nothing to work with on a boundary-less floor.
 */
    function updateToolAvailability(current: Floor): void {
        const hasBoundary = current.walls.some((wall) => wall.kind === "exterior");
        for (const tool of ["room", "opening", "box"] as const) {
            const button = document.querySelector<HTMLButtonElement>(`[data-tool="${tool}"]`);
            if (button) button.disabled = !hasBoundary;
        }
    }

    /** Adopt a document restored from either direction of the history. */
    /** The plan name and date, which live in static markup rather than in a panel
     * renderSidebar() rebuilds - so every path that changes the document has to
     * reach them here instead of getting it for free. */
    function planFields(): { name: HTMLInputElement | null; validFrom: HTMLInputElement | null } {
        return {
            name: document.getElementById("floorplan-name") as HTMLInputElement | null,
            validFrom: document.getElementById("floorplan-valid-from") as HTMLInputElement | null,
        };
    }

    /** Document <- inputs, on edit. */
    function readPlanFields(): void {
        const { name, validFrom } = planFields();
        state.doc.name = name?.value || "";
        state.doc.valid_from = validFrom?.value || null;
    }

    /** Inputs <- document, after a load or an undo. */
    function showPlanFields(): void {
        const { name, validFrom } = planFields();
        if (name) name.value = state.doc.name || "";
        if (validFrom) validFrom.value = state.doc.valid_from || "";
    }

    function applyHistoryState(doc: FloorplanDocument): void {
        state.doc = doc;
        clearSelection();
        // The restored floors array may be shorter than the one being viewed.
        state.floorIndex = Math.min(state.floorIndex, Math.max(state.doc.floors.length - 1, 0));
        showPlanFields();
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
        // Straight to a save rather than re-arming the debounce.
        retryAttempt = 0;
        void save(false);
    });
    document.getElementById("floorplan-reload")?.addEventListener("click", () => window.location.reload());
    registerLocalUndoProvider({
        canUndo: () => history.canUndo,
        canRedo: () => history.canRedo,
        undoLabel: () => (history.canUndo ? "Edit" : null),
        redoLabel: () => (history.canRedo ? "Edit" : null),
        undo,
        redo,
    });
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
 */
    /**
 * Args: item: What to select. event: The click that did it, when there was one.
 */
    function selectItem(item: SelectionItem, event: L.LeafletMouseEvent | null): void {
        const original = event?.originalEvent as MouseEvent | undefined;
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
        // A marker's own click handler and Leaflet's default bindPopup click handling fire on the same native click that got us here.
        if (item.kind === "marker") markerNodes.get(item.marker)?.openPopup();
    }

    // ---------------------------------------------------------------- render

    function render(): void {
        // A coalesced frame may still be queued - a drag's final markDirty() renders synchronously, and letting the pending one land afterwards.
        if (renderFrame !== null) {
            cancelAnimationFrame(renderFrame);
            renderFrame = null;
        }
        wallLayer.clearLayers();
        roomLayer.clearLayers();
        markerLayer.clearLayers();
        handleLayer.clearLayers();

        const current = floor();
        // Drawing a frame must never edit the document. planar.ts bridges a near-miss with a virtual edge so the region still closes while.
        const derived = deriveFaces(wallSegments(current));
        state.faces = derived.faces;

        // Once there's an exterior to read as "the building", the basemap recedes.
        mapEl.classList.toggle("has-plan", current.walls.some((wall) => wall.kind === "exterior"));

        // Each seed asked once which face is *its* room, rather than each face asking every seed whether it is inside.
        //
        // First seed wins where two land in the same face, which happens when the partition between two named rooms is deleted and they become.
        const seedForFace = new Map<Face, RoomSeed>();
        for (const room of current.rooms) {
            const bound = faceForSeed({ x: room.x, y: room.y }, derived.faces);
            if (bound && !seedForFace.has(bound)) seedForFace.set(bound, room);
        }

        // Rooms first so walls draw on top of their fills.
        const wallsById = wallIndex(current);
        roomLabels.length = 0;
        for (const face of derived.faces) {
            const seed = seedForFace.get(face);
            const roomSelected = seed ? isSelected({ kind: "room", room: seed }) : false;
            // A thicker border in a color one shade off the default teal read as nearly the same room at a glance.
            const polygon = L.polygon(face.ring.map(toLatLng), {
                className: "floorplan-room",
                ...(seed ? ROOM_FILL : UNBOUND_FILL),
                ...(roomSelected ? { color: "#f57c00", weight: 4, fillColor: "#f57c00", fillOpacity: 0.16 } : {}),
            }).addTo(roomLayer);
            // An un-subdivided outline gets no label.
            //
            // Nor does anything while a drag is running.
            if (!activeDrags && (seed || !isBuildingShell(face, wallsById))) {
                const label = seed ? seed.name || "Unnamed" : "Unnamed room";
                // Permanent, not on hover: a room appearing and naming its own area the instant a loop closes is what teaches the wall-first model,.
                polygon.bindTooltip(`<span class="floorplan-room-label__name">${escHtml(label)}</span><span class="floorplan-room-label__area">${face.area.toFixed(1)} m²</span>`, {
                    direction: "center",
                    className: "floorplan-room-label",
                    permanent: true,
                });
                roomLabels.push({ polygon, ring: face.ring });
            }
            if (seed && roomSelected && state.multi.length === 1) renderRoomRotateGrip(seed, face);
            polygon.on("click", (event) => {
                // Checked before stopping propagation: a room fill covers a large area, and a stop here regardless of tool silently swallowed every.
                if (state.tool !== "select") return;
                // An un-subdivided outline is the building, not a room inside it.
                if (!seed && isBuildingShell(face)) return;
                L.DomEvent.stop(event);
                const bound = seed || addSeedAt(interiorPoint(face.ring));
                selectItem({ kind: "room", room: bound }, event);
            });
            polygon.on("contextmenu", (event) => {
                if (state.tool !== "select") return;
                if (!seed && isBuildingShell(face)) {
                    // Still nameable, but deliberately: the context menu offers it rather than a stray click doing it.
                    L.DomEvent.stop(event);
                    pendingShellFace = face;
                    showContextMenu(event, null);
                    return;
                }
                const bound = seed || addSeedAt(interiorPoint(face.ring));
                showContextMenu(event, { kind: "room", room: bound });
            });
            // Dragging an already-selected room moves it as a whole.
            let roomDrag: {
                local: Pt;
                boundary: RoomBoundary;
                origins: Map<Wall, { ax: number; ay: number; bx: number; by: number }>;
                anchors: CornerAnchors;
                seedOrigin: Pt;
                /** Last translation that did not overlap another room - held onto so an over-far drag freezes instead of tunnelling through. */
                lastSafe: Pt;
            } | null = null;
            bindDrag(polygon.getElement(), {
                start: (event) => {
                    if (state.tool !== "select" || !seed) return false;
                    // A plain press only selects; dragging a room you have not selected yet would move the building out from under a gesture that meant.
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
                        checkpoint();
                        // A wall this room only borders never moves, so it is already safe to drag against.
                        const detached = detachSharedWalls(current, boundary, state.faces);
                        const moving: RoomBoundary = { face: boundary.face, unique: detached, shared: boundary.shared };
                        const origins = new Map<Wall, { ax: number; ay: number; bx: number; by: number }>();
                        for (const wall of [...moving.unique, ...moving.shared]) origins.set(wall, { ax: wall.ax, ay: wall.ay, bx: wall.bx, by: wall.by });
                        // Anchored against the original (pre-detach) boundary.
                        roomDrag = { local, boundary: moving, origins, anchors: cornerAnchors(boundary), seedOrigin: { x: bound.x, y: bound.y }, lastSafe: { x: 0, y: 0 } };
                    }
                    const { boundary, origins, seedOrigin } = roomDrag;
                    let dx = local.x - roomDrag.local.x;
                    let dy = local.y - roomDrag.local.y;
                    if (modifiers.constrain) {
                        const squared = constrainToAxis({ x: dx, y: dy }, (state.doc.rotation_degrees * Math.PI) / 180);
                        dx = squared.x;
                        dy = squared.y;
                    }
                    // Only the room's own walls are excluded, because only they move.
                    const carried = new Set(boundary.unique.map((item) => wallId(item)));
                    const corners: Pt[] = [];
                    for (const item of boundary.unique) {
                        const origin = origins.get(item) as { ax: number; ay: number; bx: number; by: number };
                        corners.push({ x: origin.ax, y: origin.ay }, { x: origin.bx, y: origin.by });
                    }
                    const snapped = snapDragTranslation(corners, { x: dx, y: dy }, carried);
                    dx = snapped.x;
                    dy = snapped.y;
                    // A room's own walls can still swing into a room that was never adjacent to begin with (dragged clean across a hall).
                    //
                    // A freshly detached wall starts exactly on the boundary it used to share, which a plain point-in-polygon test cannot reliably call.
                    const drag = roomDrag;
                    const candidate: Pt[] = [];
                    for (const corner of corners) candidate.push(anchoredMove(corner, dx, dy, drag));
                    const NUDGE_METERS = 0.05;
                    const seedCandidate: Pt = { x: seedOrigin.x + dx, y: seedOrigin.y + dy };
                    const nudged = candidate.map((corner) => {
                        const towardX = seedCandidate.x - corner.x;
                        const towardY = seedCandidate.y - corner.y;
                        const len = Math.hypot(towardX, towardY);
                        if (len < 1e-9) return corner;
                        const shift = Math.min(NUDGE_METERS, len);
                        return { x: corner.x + (towardX / len) * shift, y: corner.y + (towardY / len) * shift };
                    });
                    const others = occupiedFaces(current, state.faces)
                        .filter((entry) => entry.room !== bound)
                        .map((entry) => entry.face);
                    if (others.some((face) => nudged.some((point) => pointInRing(point, face.ring)))) {
                        dx = roomDrag.lastSafe.x;
                        dy = roomDrag.lastSafe.y;
                    } else {
                        roomDrag.lastSafe = { x: dx, y: dy };
                    }
                    for (const wall of boundary.unique) {
                        const orig = origins.get(wall) as { ax: number; ay: number; bx: number; by: number };
                        const a = anchoredMove({ x: orig.ax, y: orig.ay }, dx, dy, roomDrag);
                        const b = anchoredMove({ x: orig.bx, y: orig.by }, dx, dy, roomDrag);
                        wall.ax = a.x;
                        wall.ay = a.y;
                        wall.bx = b.x;
                        wall.by = b.y;
                    }
                    bound.x = seedOrigin.x + dx;
                    bound.y = seedOrigin.y + dy;
                    renderSoon();
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
            const a = wallStart(wall);
            const b = wallEnd(wall);
            const along = (t: number): Pt => ({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t });
            // A door/doorway/hatch renders as an actual break in the wall - one solid segment per interval left once its openings are cut out.
            for (const [s0, s1] of wallSolidIntervals(wall)) {
                const line = L.polyline([toLatLng(along(s0)), toLatLng(along(s1))], {
                    className: "floorplan-wall",
                    ...style,
                    ...(selected ? { color: "#f57c00", weight: (style?.weight || 3) + 2 } : {}),
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
                // Dragging the wall's body moves the whole wall, not just one endpoint.
                bindDrag(line.getElement(), {
                    start: (event) => {
                        if (state.tool !== "select") return false;
                        return true;
                    },
                    move: ({ local, modifiers }) => {
                        if (!dragOrigin) {
                            dragOrigin = { local, a: wallStart(wall), b: wallEnd(wall) };
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
                        // Everything this drag rewrites, not just the wall under the cursor.
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
                        renderSoon();
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
        if (state.tool === "select" && !activeDrags) {
            renderJointHandles(current);
            renderMidpointHandles(current);
        }

        markerNodes.clear();
        for (const marker of current.markers) {
            const selected = isSelected({ kind: "marker", marker });
            const node = L.marker(toLatLng({ x: marker.x, y: marker.y }), { icon: markerIcon(marker, selected), draggable: state.tool === "select" }).addTo(markerLayer);
            markerNodes.set(marker, node);
            // Built when the popup opens, not when the marker is drawn. render() runs on every frame of a drag, and markerPopupContent assembles.
            node.bindPopup(() => markerPopupContent(marker), { closeButton: true });
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
        updateDeleteButton();
        updateEmptyState(current);
        updateToolAvailability(current);
        scheduleRoomLabelFit();
    }

    let renderFrame: number | null = null;

    /**
 * Render at most once per displayed frame.
 */
    function renderSoon(): void {
        if (renderFrame !== null) return;
        renderFrame = requestAnimationFrame(() => {
            renderFrame = null;
            render();
        });
    }

    let labelFitFrame: number | null = null;

    /**
 * Re-fit the room labels once per frame at most.
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
            // The screen-space bounding box, which for a concave room is more generous than the room itself.
            const fits = element.offsetWidth <= maxX - minX && element.offsetHeight <= maxY - minY;
            decisions.push({ element, fits });
        }
        for (const { element, fits } of decisions) element.classList.toggle("is-clipped", !fits);
    }

    /**
 * The parts of a wall's centreline still solid, once its door/doorway/ hatch openings are cut out of it.
 */
    /**
 * The wall nearest a point, within grabbing distance.
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

    function wallSolidIntervals(wall: Wall): Array<[number, number]> {
        const gaps = wall.openings
            // A window is the exception: you cannot walk through one, so it draws over an unbroken wall rather than cutting it.
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
        const a = wallStart(wall);
        const b = wallEnd(wall);
        const at = (t: number): Pt => ({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t });
        for (const opening of wall.openings) {
            const openingSelected = isSelected({ kind: "opening", wall, opening });
            // A window stays a colored line over an otherwise-continuous wall - it doesn't let anyone through, so a break would be the wrong signal.
            const isWindow = opening.kind === "window";
            // The plan symbol for a door: the leaf at its open position and the quarter it sweeps.
            for (const leaf of doorLeaves(wall, opening)) {
                L.polyline(leaf.map(toLatLng), {
                    className: "floorplan-door-swing",
                    color: openingSelected ? "#f57c00" : "#fb8c00",
                    weight: 1.5,
                    opacity: openingSelected ? 0.9 : 0.5,
                    interactive: false,
                }).addTo(wallLayer);
            }
            const line = L.polyline([toLatLng(at(opening.t_start)), toLatLng(at(opening.t_end))], {
                className: "floorplan-opening",
                color: openingSelected ? "#f57c00" : isWindow ? "#1e88e5" : "#fb8c00",
                weight: openingSelected ? 9 : 6,
                opacity: openingSelected || isWindow ? 1 : 0.45,
            })
                .bindTooltip(opening.kind, { direction: "top" })
                .addTo(wallLayer);
            // Selectable independently of its wall, so an opening can be clicked and deleted (keyboard Delete, like every other selectable item).
            line.on("click", (event) => {
                if (state.tool !== "select") return;
                L.DomEvent.stop(event);
                selectItem({ kind: "opening", wall, opening }, event);
            });
            line.on("contextmenu", (event) => {
                if (state.tool !== "select") return;
                showContextMenu(event, { kind: "opening", wall, opening });
            });
            // Dragging the line itself slides the opening along its wall, keeping its width fixed.
            let slide: { startT: number; width: number; originalStart: number; host: Wall } | null = null;
            bindDrag(line.getElement(), {
                start: () => state.tool === "select",
                move: ({ local, modifiers }) => {
                    if (!slide) {
                        slide = { startT: projectOnSegment(local, a, b).t, width: opening.t_end - opening.t_start, originalStart: opening.t_start, host: wall };
                        checkpoint();
                    }
                    // Dragged onto another wall, the opening goes with it - a door put on the wrong side of a room was otherwise a delete and a redraw.
                    const host = slide.host;
                    const hostAway = projectOnSegment(local, { x: host.ax, y: host.ay }, { x: host.bx, y: host.by }).distance;
                    const nearest = modifiers.less ? null : wallNear(local);
                    // Strictly closer, not merely different.
                    const target = nearest && nearest.wall !== host && nearest.distance < hostAway ? nearest.wall : null;
                    if (target) {
                        const targetA = { x: target.ax, y: target.ay };
                        const targetB = { x: target.bx, y: target.by };
                        const along = projectOnSegment(local, targetA, targetB).t;
                        // A wall with no length refuses the move.
                        if (rehostOpening(opening, host, target, along * wallLength(target))) {
                            slide = { startT: along, width: opening.t_end - opening.t_start, originalStart: opening.t_start, host: target };
                            if (isSelected({ kind: "opening", wall: host, opening })) {
                                state.selection = { kind: "opening", wall: target, opening };
                                state.multi = [state.selection];
                            }
                            renderSoon();
                            return;
                        }
                    }
                    const hostA = { x: host.ax, y: host.ay };
                    const hostB = { x: host.bx, y: host.by };
                    const currentT = projectOnSegment(local, hostA, hostB).t;
                    const start = Math.max(0, Math.min(slide.originalStart + (currentT - slide.startT), 1 - slide.width));
                    opening.t_start = start;
                    opening.t_end = start + slide.width;
                    renderSoon();
                },
                end: (moved) => {
                    slide = null;
                    if (moved) markDirty();
                },
            });
            // The opening's own selection, not the wall's - selecting a door replaces the whole selection with {kind.
            if (openingSelected) renderOpeningHandles(wall, opening, at);
        }
    }

    /**
 * Draggable ends for one opening, shown only while its wall is selected.
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
                    const along = projectOnSegment(local, wallStart(wall), wallEnd(wall)).t;
                    if (end === "t_start") opening.t_start = Math.min(along, opening.t_end - MIN_WIDTH);
                    else opening.t_end = Math.max(along, opening.t_start + MIN_WIDTH);
                    opening.t_start = Math.max(0, opening.t_start);
                    opening.t_end = Math.min(1, opening.t_end);
                    renderSoon();
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
 */
    function renderUnderlay(): void {
        underlayLayer.clearLayers();
        const current = floor();
        const below = state.doc.floors.filter((item) => item.level < current.level).sort((x, y) => y.level - x.level)[0];
        // Offering "Floor below" when there is none to show is worse than no toggle at all.
        document.querySelector<HTMLElement>('#floorplan-layers [data-map-layer="underlay"]')?.toggleAttribute("hidden", !below);
        if (!state.showUnderlay || !below) return;
        for (const wall of below.walls) {
            L.polyline([toLatLng(wallStart(wall)), toLatLng(wallEnd(wall))], {
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
 * The visible measurement grid, squared to the plan's own drawing axis.
 */
    function renderGrid(): void {
        gridLayer.clearLayers();
        if (!state.showGrid) return;
        const axisRadians = (state.doc.rotation_degrees * Math.PI) / 180;
        // Into axis-space before finding the extent, so the grid squares to the drawing axis rather than to true north.
        const corners = [map.getBounds().getNorthWest(), map.getBounds().getNorthEast(), map.getBounds().getSouthEast(), map.getBounds().getSouthWest()].map((ll) =>
            rotate(toLocal(ll), -axisRadians),
        );
        const minX = Math.min(...corners.map((p) => p.x));
        const maxX = Math.max(...corners.map((p) => p.x));
        const minY = Math.min(...corners.map((p) => p.y));
        const maxY = Math.max(...corners.map((p) => p.y));

        // Coarsen rather than draw thousands of lines when zoomed out far enough to see a whole neighbourhood.
        let spacing = GRID_SPACING_METERS;
        const MAX_LINES = 400;
        while ((maxX - minX) / spacing + (maxY - minY) / spacing > MAX_LINES) spacing *= 2;

        const lines: Array<[[number, number], [number, number]]> = [];
        for (let x = Math.floor(minX / spacing) * spacing; x <= maxX; x += spacing) {
            lines.push([toLatLng(rotate({ x, y: minY }, axisRadians)), toLatLng(rotate({ x, y: maxY }, axisRadians))]);
        }
        for (let y = Math.floor(minY / spacing) * spacing; y <= maxY; y += spacing) {
            lines.push([toLatLng(rotate({ x: minX, y }, axisRadians)), toLatLng(rotate({ x: maxX, y }, axisRadians))]);
        }
        L.polyline(lines, { color: "#90a4ae", weight: 1, opacity: 0.35, interactive: false }).addTo(gridLayer);
    }

    /**
 * A handle for turning a selected room.
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

        L.polyline([toLatLng(pivot), toLatLng(gripAt)], { color: "#f57c00", weight: 1, dashArray: "3 3", interactive: false }).addTo(handleLayer);
        const grip = L.circleMarker(toLatLng(gripAt), { radius: 7, color: "#f57c00", fillColor: "#fff", fillOpacity: 1, weight: 2, className: "floorplan-handle floorplan-rotate-grip" }).addTo(handleLayer);

        let turning: { start: number; walls: Map<Wall, { a: Pt; b: Pt }>; seedAt: Pt; anchors: CornerAnchors } | null = null;
        bindDrag(grip.getElement(), {
            move: ({ local }) => {
                if (!turning) {
                    const walls = new Map<Wall, { a: Pt; b: Pt }>();
                    for (const wall of [...boundary.unique, ...boundary.shared]) walls.set(wall, { a: wallStart(wall), b: wallEnd(wall) });
                    turning = { start: Math.atan2(local.y - pivot.y, local.x - pivot.x), walls, seedAt: { x: seed.x, y: seed.y }, anchors: cornerAnchors(boundary) };
                    checkpoint();
                }
                const now = Math.atan2(local.y - pivot.y, local.x - pivot.x);
                // Suspending snap gives a free angle; otherwise it steps, since turning a room by hand is an attempt to line it up with something.
                const angle = snapOff() ? now - turning.start : snapRotation(now - turning.start);
                // A corner resting on a wall the room does not own stays on it, sliding rather than dragging.
                const turned = (corner: Pt): Pt => {
                    const moved = rotate(corner, angle, pivot);
                    const resting = (turning as NonNullable<typeof turning>).anchors.get(cornerKey(corner));
                    if (!resting) return moved;
                    return projectOnSegment(moved, wallStart(resting), wallEnd(resting)).point;
                };
                for (const wall of boundary.unique) {
                    const origin = turning.walls.get(wall) as { a: Pt; b: Pt };
                    const a = turned(origin.a);
                    const b = turned(origin.b);
                    wall.ax = a.x;
                    wall.ay = a.y;
                    wall.bx = b.x;
                    wall.by = b.y;
                }
                // The seed turns with the room, so the region keeps the name it was given rather than rebinding to whatever now sits over the point it.
                const movedSeed = rotate(turning.seedAt, angle, pivot);
                seed.x = movedSeed.x;
                seed.y = movedSeed.y;
                const readout = document.getElementById("floorplan-hint");
                if (readout) readout.textContent = `${Math.round((angle * 180) / Math.PI)}°`;
                renderSoon();
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
 * Every (wall, end) reachable from `seed`'s own two endpoints by following shared corners.
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
                    queue.push(wallEnd(wall));
                }
                if (wall.bx === point.x && wall.by === point.y) {
                    result.push({ wall, end: "b", origX: wall.bx, origY: wall.by });
                    queue.push(wallStart(wall));
                }
            }
        }
        return result;
    }

    /**
 * Whether a region is the building's own shell rather than a room in it.
 */
    /** This floor's walls by id, so a per-face lookup is not a per-face scan. */
    function wallIndex(current: Floor = floor()): Map<string, Wall> {
        return new Map(current.walls.map((wall) => [wallId(wall), wall] as const));
    }

    /**
 * Args: face: The derived region. byId: This floor's walls by id.
 */
    function isBuildingShell(face: Face, byId: Map<string, Wall> = wallIndex()): boolean {
        let seen = 0;
        for (const id of face.wallIds) {
            const wall = byId.get(id);
            if (!wall) continue;
            if (wall.kind !== "exterior") return false;
            seen += 1;
        }
        return seen > 0;
    }

    /** Which wall, if any, each of a room's own corners is resting against. */
    type CornerAnchors = Map<string, Wall>;

    /** A corner's key, exact rather than rounded - these are the authored values. */
    const cornerKey = (p: Pt): string => `${p.x},${p.y}`;

    /**
 * Find, for each of the room's own corners, the wall it is sitting on.
 */
    function cornerAnchors(boundary: RoomBoundary): CornerAnchors {
        const anchors: CornerAnchors = new Map();
        if (!boundary.shared.length) return anchors;
        const tolerance = 1e-6;
        for (const wall of boundary.unique) {
            for (const corner of [wallStart(wall), wallEnd(wall)]) {
                const resting = boundary.shared.find(
                    (other) => projectOnSegment(corner, wallStart(other), wallEnd(other)).distance <= tolerance,
                );
                if (resting) anchors.set(cornerKey(corner), resting);
            }
        }
        return anchors;
    }

    /**
 * Translate one of the room's corners, keeping it on whatever it rests on.
 */
    function anchoredMove(corner: Pt, dx: number, dy: number, held: { anchors: CornerAnchors }): Pt {
        const moved = { x: corner.x + dx, y: corner.y + dy };
        const resting = held.anchors.get(cornerKey(corner));
        if (!resting) return moved;
        return projectOnSegment(moved, wallStart(resting), wallEnd(resting)).point;
    }

    /**
 * Every distinct corner on a floor, with the wall ends that meet there.
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
            add(wallStart(wall), wall, "a");
            add(wallEnd(wall), wall, "b");
        }
        return joints;
    }

    /**
 * Draw every joint on the floor, and let each one be dragged.
 */
    function renderJointHandles(current: Floor): void {
        const selectedWalls = new Set(state.multi.filter((item) => item.kind === "wall").map((item) => wallId(item.wall)));
        for (const joint of wallJoints(current).values()) {
            const onSelection = joint.ends.some((entry) => selectedWalls.has(wallId(entry.wall)));
            const handle = L.circleMarker(toLatLng(joint.point), {
                radius: onSelection ? 6 : 3.5,
                color: "#f57c00",
                fillColor: "#fff",
                fillOpacity: 1,
                weight: onSelection ? 2 : 1,
                className: "floorplan-handle floorplan-joint",
            }).addTo(handleLayer);

            handle.on("contextmenu", (event) => {
                L.DomEvent.stop(event);
                closeContextMenu();
                const menu = buildJointContextMenu(joint);
                const original = event.originalEvent as MouseEvent;
                menu.style.left = `${original.clientX}px`;
                menu.style.top = `${original.clientY}px`;
                document.body.appendChild(menu);
                contextMenuEl = menu;
            });

            // Captured on the first move: the ends are read off the geometry as it stands now, and re-reading them mid-drag would pick up walls that.
            let moving: Array<{ wall: Wall; end: "a" | "b" }> | null = null;
            bindDrag(handle.getElement(), {
                start: () => state.tool === "select",
                move: ({ local, modifiers }) => {
                    if (!moving) {
                        moving = joint.ends;
                        checkpoint();
                    }
                    // Every wall on this joint travels with it, so none of them
                    // can be what it snaps to.
                    const carried = new Set(moving.map((entry) => wallId(entry.wall)));
                    const others = wallSegments(current).filter((segment) => !carried.has(segment.wallId));
                    // Shift locks the corner to one axis of the plan, the same thing it does to a wall body and to a room.
                    let aimed = local;
                    if (modifiers.constrain) {
                        const squared = constrainToAxis({ x: local.x - joint.point.x, y: local.y - joint.point.y }, (state.doc.rotation_degrees * Math.PI) / 180);
                        aimed = { x: joint.point.x + squared.x, y: joint.point.y + squared.y };
                    }
                    const snapped = snapPoint(aimed, others, tolerances(), { suspended: snapOff(), grid: gridOption() });
                    for (const entry of moving) {
                        if (entry.end === "a") {
                            entry.wall.ax = snapped.point.x;
                            entry.wall.ay = snapped.point.y;
                        } else {
                            entry.wall.bx = snapped.point.x;
                            entry.wall.by = snapped.point.y;
                        }
                    }
                    renderSoon();
                },
                end: (moved) => {
                    moving = null;
                    if (moved) markDirty();
                },
            });
        }
    }

    /**
 * A handle at a selected wall's own middle - dragging it splits the wall in two, so a bend can be added to a straight run without.
 */
    function renderMidpointHandles(current: Floor): void {
        for (const wall of current.walls) {
            if (wall.openings.length) continue;
            if (!isSelected({ kind: "wall", wall })) continue;
            const mid = { x: (wall.ax + wall.bx) / 2, y: (wall.ay + wall.by) / 2 };
            const handle = L.circleMarker(toLatLng(mid), {
                radius: 3,
                color: "#f57c00",
                fillColor: "#fff",
                fillOpacity: 0.6,
                weight: 1,
                className: "floorplan-handle floorplan-wall-midpoint",
            }).addTo(handleLayer);

            // Set on the first move: the wall is split right there, and every move after that is an ordinary two-wall joint drag on the corner.
            let split: { near: Wall; far: Wall } | null = null;
            bindDrag(handle.getElement(), {
                start: () => state.tool === "select",
                move: ({ local, modifiers }) => {
                    if (!split) {
                        checkpoint();
                        const near: Wall = { ...wall, uuid: nextLocalId(), bx: mid.x, by: mid.y, openings: [] };
                        const far: Wall = { ...wall, uuid: nextLocalId(), ax: mid.x, ay: mid.y, openings: [] };
                        current.walls = current.walls.filter((item) => item !== wall);
                        current.walls.push(near, far);
                        split = { near, far };
                    }
                    const carried = new Set([wallId(split.near), wallId(split.far)]);
                    const others = wallSegments(current).filter((segment) => !carried.has(segment.wallId));
                    let aimed = local;
                    if (modifiers.constrain) {
                        const squared = constrainToAxis({ x: local.x - mid.x, y: local.y - mid.y }, (state.doc.rotation_degrees * Math.PI) / 180);
                        aimed = { x: mid.x + squared.x, y: mid.y + squared.y };
                    }
                    const snapped = snapPoint(aimed, others, tolerances(), { suspended: snapOff(), grid: gridOption() });
                    split.near.bx = snapped.point.x;
                    split.near.by = snapped.point.y;
                    split.far.ax = snapped.point.x;
                    split.far.ay = snapped.point.y;
                    renderSoon();
                },
                end: (moved) => {
                    split = null;
                    if (moved) markDirty();
                },
            });
        }
    }

    // ------------------------------------------------------------ popups

    // A click that lands elsewhere on the map while a marker's popup is open closes that popup (Leaflet's own default).
    let popupOpenCount = 0;
    map.on("popupopen", () => {
        popupOpenCount++;
    });
    map.on("popupclose", () => {
        popupOpenCount = Math.max(0, popupOpenCount - 1);
    });
    // pointerdown, not mousedown: a finger never fires mousedown, so on touch this read false and a tap whose only job was to dismiss.
    let popupOpenAtPointerDown = false;
    map.getContainer().addEventListener(
        "pointerdown",
        () => {
            popupOpenAtPointerDown = popupOpenCount > 0;
        },
        true,
    );

    // ----------------------------------------------------------- box select

    let boxStart: L.Point | null = null;
    let boxActive = false;
    let boxRectEl: HTMLDivElement | null = null;
    // A box-select's mouseup still reaches map.on("click") below.
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
        // Tested in screen pixels, not in latitude and longitude.
        const minX = Math.min(from.x, to.x);
        const maxX = Math.max(from.x, to.x);
        const minY = Math.min(from.y, to.y);
        const maxY = Math.max(from.y, to.y);
        const inside = (point: Pt): boolean => {
            const pixel = map.latLngToContainerPoint(toLatLng(point));
            return pixel.x >= minX && pixel.x <= maxX && pixel.y >= minY && pixel.y <= maxY;
        };

        const matches: SelectionItem[] = [];
        for (const wall of floor().walls) {
            if (inside(wallStart(wall)) && inside(wallEnd(wall))) matches.push({ kind: "wall", wall });
        }
        for (const marker of floor().markers) {
            if (inside({ x: marker.x, y: marker.y })) matches.push({ kind: "marker", marker });
        }
        for (const room of floor().rooms) {
            if (inside({ x: room.x, y: room.y })) matches.push({ kind: "room", room });
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

    // Box select is the Box tool's gesture, and only its gesture.
    //
    // Shift+drag used to start one from the Select tool as well, over anything including a wall.
    //
    // One meaning per modifier is worth more than a second way in to a tool that already has its own button.
    map.getContainer().addEventListener("mousedown", (event: MouseEvent) => {
        if (state.tool !== "box" || event.button !== 0) return;
        const target = event.target as HTMLElement;
        // Ordinary wall/room/marker shapes are NOT excluded here - only things with their own competing drag behavior are.
        if (target.closest(".leaflet-marker-icon, .floorplan-handle, .leaflet-popup, .leaflet-control, .floorplan-context-menu")) return;
        map.dragging.disable();
        boxStart = map.mouseEventToContainerPoint(event);
        boxActive = false;
    });
    // On window, not the container.
    window.addEventListener("mousemove", (event: MouseEvent) => {
        if (!boxStart) return;
        const current = map.mouseEventToContainerPoint(event);
        if (!boxActive) {
            // A few pixels of slop before committing to box-select, so an ordinary click on empty space.
            if (boxStart.distanceTo(current) < 6) return;
            boxActive = true;
            map.dragging.disable();
            boxRectEl = document.createElement("div");
            boxRectEl.className = "floorplan-box-select";
            map.getContainer().appendChild(boxRectEl);
        }
        drawBoxRect(boxStart, current);
    });
    window.addEventListener("mouseup", (event: MouseEvent) => {
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

    // Click-and-drag draws a single wall directly.
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
        wallDragStartLocal = snapPoint(toLocal(map.containerPointToLatLng(wallDragStartPixel)), wallSegments(floor()), tolerances(), { suspended: snapOff(), grid: gridOption() }).point;
        wallDragActive = false;
    });
    map.getContainer().addEventListener("mousemove", (event: MouseEvent) => {
        if (!wallDragStartPixel || !wallDragStartLocal) return;
        const current = map.mouseEventToContainerPoint(event);
        if (!wallDragActive) {
            // A few pixels of slop so an ordinary click that starts the click-click chain (which also begins here, since it might become a drag).
            if (wallDragStartPixel.distanceTo(current) < 6) return;
            wallDragActive = true;
        }
        const snapped = snapPoint(toLocal(map.containerPointToLatLng(current)), wallSegments(floor()), tolerances(), {
            from: wallDragStartLocal,
            suspended: snapOff(),
            axisRadians: (state.doc.rotation_degrees * Math.PI) / 180,
            grid: gridOption(),
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
                grid: gridOption(),
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
    /** The un-subdivided outline a context menu was opened over, if any. */
    let pendingShellFace: Face | null = null;
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
            if (pendingShellFace) {
                const face = pendingShellFace;
                addAction("Name this space", () => {
                    const seed = addSeedAt(interiorPoint(face.ring));
                    selectItem({ kind: "room", room: seed }, null);
                    render();
                });
            }
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
            // One entry per kind rather than a single "Add opening" that always made a door.
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

    /** The one action a joint handle offers, on its own small menu - a joint is not a SelectionItem, so it does not go through buildContextMenu. */
    function buildJointContextMenu(joint: { point: Pt; ends: Array<{ wall: Wall; end: "a" | "b" }> }): HTMLUListElement {
        const menu = document.createElement("ul");
        menu.className = "floorplan-context-menu";
        const entry = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "Remove this point";
        button.addEventListener("click", () => {
            removeJoint(joint);
            closeContextMenu();
        });
        entry.appendChild(button);
        menu.appendChild(entry);
        return menu;
    }

    /**
 * Remove a corner where exactly two walls meet, merging them into one.
 */
    function removeJoint(joint: { point: Pt; ends: Array<{ wall: Wall; end: "a" | "b" }> }): void {
        if (joint.ends.length !== 2) {
            toast.info("This point can only be removed where exactly two walls meet.");
            return;
        }
        const [first, second] = joint.ends as [{ wall: Wall; end: "a" | "b" }, { wall: Wall; end: "a" | "b" }];
        if (first.wall.openings.length || second.wall.openings.length) {
            toast.info("Remove the door, window, or gate here first.");
            return;
        }
        if (first.wall.kind !== second.wall.kind) {
            toast.info("These two walls are different kinds and can't be merged.");
            return;
        }
        checkpoint();
        const current = floor();
        const boundBefore = boundSeeds(current);
        const far = (entry: { wall: Wall; end: "a" | "b" }): Pt => (entry.end === "a" ? wallEnd(entry.wall) : wallStart(entry.wall));
        const farFirst = far(first);
        const farSecond = far(second);
        const merged: Wall = { uuid: nextLocalId(), kind: first.wall.kind, thickness: first.wall.thickness, ax: farFirst.x, ay: farFirst.y, bx: farSecond.x, by: farSecond.y, openings: [] };
        current.walls = current.walls.filter((wall) => wall !== first.wall && wall !== second.wall);
        current.walls.push(merged);
        pruneOrphanedSeeds(current, boundBefore);
        pruneUnusedReferences();
        clearSelection();
        renderSidebar();
        markDirty();
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
 * Fly to whatever is already known, instead of opening on a fixed zoom around the pin's coordinate.
 */
    function fitToContent(): void {
        const current = floor();
        const points: Pt[] = [];
        for (const wall of current.walls) points.push(wallStart(wall), wallEnd(wall));
        if (!points.length) for (const room of current.rooms) points.push({ x: room.x, y: room.y });
        if (!points.length) for (const marker of current.markers) points.push({ x: marker.x, y: marker.y });
        const latLngs = points.length ? points.map(toLatLng) : outline.length ? outline.map(([outlineLat, outlineLng]): [number, number] => [outlineLat, outlineLng]) : null;
        if (!latLngs) return;
        map.fitBounds(L.latLngBounds(latLngs), { padding: [32, 32], maxZoom: 22 });
    }

    /**
 * Lay the building's real footprint down as exterior walls.
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
 * Args: point: Where the seed goes, in plan-local metres. record: Whether this is a gesture of its own.
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
 * Learn a room size from this floor's own already-enclosed rooms.
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
 * Which compass side of its own room's bounding box a wall's midpoint falls.
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
 * Click-once room generation: a rectangle sized from this floor's own rooms (or a sensible default), its corners snapped onto whatever.
 */
    function placeRoomAt(center: Pt): void {
        const current = floor();
        // Whatever the click landed inside, before anything new is added.
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

        // The rectangle above is sized and snapped from nearby geometry, but nothing so far stops it landing on top of a room that already.
        const blockers = occupiedFaces(current, state.faces)
            .filter((entry) => entry.face !== containerFace)
            .map((entry) => entry.face);
        if (blockers.some((face) => polygonOverlapsFace(corners, face))) {
            toast.info("There's already a room there — try a different spot.");
            return;
        }

        checkpoint();
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
            // Whichever of this room's own walls best matches the learned side.
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
                    // The first wall on an empty floor is the shell whatever the panel says - nobody starts a plan with an interior partition.
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

    /**
 * Move the wall tool's preview to wherever the pointer is.
 */
    function aimWallPreview(latlng: L.LatLng): void {
        if (state.tool !== "wall") return;
        const raw = toLocal(latlng);
        const from = state.drawing.length ? (state.drawing[state.drawing.length - 1] as Pt) : null;
        const snapped = snapPoint(raw, wallSegments(floor()), tolerances(), {
            from,
            suspended: snapOff(),
            axisRadians: (state.doc.rotation_degrees * Math.PI) / 180,
            grid: gridOption(),
        });
        state.cursor = snapped.point;
        state.snapKind = snapped.label;
        drawGhost();
    }

    // Pointer events rather than Leaflet's mousemove, which a finger never emits.
    //
    // A finger has no hover, so pointermove only arrives while it is down - which turns out to be the useful gesture anyway.
    for (const type of ["pointerdown", "pointermove"] as const) {
        map.getContainer().addEventListener(type, (raw) => {
            const event = raw as PointerEvent;
            if (state.tool !== "wall") return;
            // Mouse already gets a continuous pointermove while it hovers, so pointerdown adds nothing for it.
            if (type === "pointerdown" && event.pointerType === "mouse") return;
            aimWallPreview(map.mouseEventToLatLng(event));
        });
    }

    // ...but that lift has to place the corner itself.
    let touchAim: { id: number; cancelled: boolean; wasDraggable: boolean } | null = null;

    map.getContainer().addEventListener("pointerdown", (raw) => {
        const event = raw as PointerEvent;
        // A suppression only ever applies to the click of the gesture that asked for it.
        suppressNextClick = false;
        if (event.pointerType === "mouse" || state.tool !== "wall") return;
        // Leaflet's controls sit inside the map container and stop their own click from reaching the map.
        if ((event.target as Element | null)?.closest?.(".leaflet-marker-icon, .floorplan-handle, .leaflet-popup, .leaflet-control, .floorplan-context-menu")) return;
        if (touchAim) {
            touchAim.cancelled = true;
            return;
        }
        touchAim = { id: event.pointerId, cancelled: false, wasDraggable: map.dragging.enabled() };
        // One finger aims instead of panning while the wall tool is armed.
        map.dragging.disable();
    });

    const endTouchAim = (event: PointerEvent, place: boolean): void => {
        if (!touchAim || event.pointerId !== touchAim.id) return;
        const { cancelled, wasDraggable } = touchAim;
        touchAim = null;
        if (wasDraggable) map.dragging.enable();
        if (cancelled || !place) return;
        // A lift that did not move still fires a click, and that click would otherwise run the whole thing a second time.
        suppressNextClick = true;
        if (popupOpenAtPointerDown) {
            popupOpenAtPointerDown = false;
            return;
        }
        tapMap(map.mouseEventToLatLng(event));
    };
    // On window, not the container: a finger that slides off the map still has
    // to finish the corner it was aiming.
    window.addEventListener("pointerup", (raw) => endTouchAim(raw as PointerEvent, true));
    window.addEventListener("pointercancel", (raw) => endTouchAim(raw as PointerEvent, false));

    /**
 * Act on a tap at a map position: place, cut, or select, per the armed tool.
 */
    function tapMap(latlng: L.LatLng): void {
        const raw = toLocal(latlng);
        if (state.tool === "wall") {
            const from = state.drawing.length ? (state.drawing[state.drawing.length - 1] as Pt) : null;
            const snapped = snapPoint(raw, wallSegments(floor()), tolerances(), {
                from,
                suspended: snapOff(),
                axisRadians: (state.doc.rotation_degrees * Math.PI) / 180,
                grid: gridOption(),
            });
            // Clicking the chain's own origin closes the loop and finishes.
            const origin = state.drawing[0];
            const closeTolerance = 12 * metresPerPixel();
            if (origin && state.drawing.length >= 2 && distance(snapped.point, origin) < closeTolerance) {
                state.drawing.push(origin);
                commitChain();
                return;
            }
            // Clicking the chain's own last point again finishes it open-ended (no closing segment).
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
            // A fixed 0.9m, not a fixed fraction of the wall.
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
            // Select what was just placed: naming it, or linking a stair to the floor below, is almost always the next thing wanted, and making.
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
    }

    map.on("click", (event: L.LeafletMouseEvent) => {
        // A box-select drag ends in a mouseup that Leaflet still reads as a click.
        if (suppressNextClick) {
            suppressNextClick = false;
            return;
        }
        if (popupOpenAtPointerDown) {
            popupOpenAtPointerDown = false;
            return;
        }
        tapMap(event.latlng);
    });

    map.on("dblclick", () => {
        if (state.tool === "wall") commitChain();
    });

    map.on("contextmenu", (event: L.LeafletMouseEvent) => {
        if (state.tool !== "select") return;
        pendingContextPoint = toLocal(event.latlng);
        pendingShellFace = null;
        showContextMenu(event, null);
    });

    // The same value drives wall angle-snapping and the map's own visual rotation, so turning the map to face a building also squares.
    map.getContainer().addEventListener("pointerdown", (raw) => {
        const event = raw as PointerEvent;
        if (state.tool !== "rotate" || !canRotateView) return;
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
            // The release reads as a click on the canvas, which would otherwise fall through to "clicked empty space" and drop the selection.
            if (turned) suppressNextClick = true;
        };
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onFinish);
        window.addEventListener("pointercancel", onFinish);
    });

    // A zoom changes what fits without changing anything worth re-rendering.
    map.on("zoomend", () => scheduleRoomLabelFit());

    // The grid depends only on the viewport and the axis, both of which these three events cover between them.
    map.on("moveend zoomend rotate", renderGrid);

    map.on("rotate", () => {
        const bearing = map.getBearing();
        // Also fires for the load-time setBearing() that restores a saved plan's rotation.
        if (Math.abs(bearing - state.doc.rotation_degrees) < 1e-6) return;
        checkpoint("rotate");
        state.doc.rotation_degrees = bearing;
        markDirtyQuiet();
    });

    document.addEventListener("keydown", (event) => {
        const target = event.target as HTMLElement | null;
        // Every hotkey below is a bare letter (mnemonics: W for Wall, and so on).
        const typing = isTypingTarget(target);

        // Backtick, not Alt: Alt now means "take less" for a drag, and a key that both detaches a wall and disables snapping is a key whose.
        if (event.key === "`" || event.code === "Backquote") state.suspendSnap = true;
        const onCanvas = document.activeElement === mapEl || mapEl.contains(document.activeElement);

        // Tab steps through the plan while the canvas has focus.
        if (event.key === "Tab" && onCanvas && !typing) {
            const items = selectableItems();
            if (items.length) {
                event.preventDefault();
                stepSelection(event.shiftKey ? -1 : 1);
                return;
            }
        }

        // Arrows nudge the selection. With nothing selected they fall through
        if (event.key === "Escape") {
            closeContextMenu();
            // Blurring last, once there is nothing left to clear, so Escape
            // reads as "back out one level" rather than "leave immediately".
            if (onCanvas && !state.drawing.length && state.tool === "select" && !state.selection) {
                mapEl.blur();
                return;
            }
            // Finishes the chain rather than throwing it away.
            if (state.drawing.length) commitChain();
            // Escape leaves a tool before it clears a selection.
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
        // Ctrl/Cmd+Z and Ctrl/Cmd+Shift+Z / Ctrl+Y are handled by the shared undo bar, which also drives the floating buttons.
        if (key === "v") setTool("select");
        if (key === "b") setTool("box");
        if (key === "d") setTool("opening");
        // Its button is removed when leaflet-rotate is missing; the shortcut has to
        // go with it, or the key arms a tool nothing on screen shows or acts on.
        if (key === "t" && canRotateView) setTool("rotate");
        if (key === "w") setTool("wall");
        if (key === "r") setTool("room");
        if (key === "m") setTool("marker");
        // One table, read here and shown in the options panel, so the key that
        // arms a kind and the key the panel advertises cannot disagree.
        for (const [kind, shortcut] of Object.entries(MARKER_KEYS) as Array<[MarkerKind, string]>) {
            if (key !== shortcut) continue;
            state.markerKind = kind;
            setTool("marker");
        }
    });
    // Arrow keys, taken on the canvas in the capture phase so this runs before
    // Leaflet's own keydown listener on the same element.
    //
    // Leaflet pans the map with the arrows.
    //
    // Stopping propagation here rather than disabling Leaflet's handler, because that handler only listens while it believes the container.
    mapEl.addEventListener(
        "keydown",
        (raw) => {
            const event = raw as KeyboardEvent;
            if (!event.key.startsWith("Arrow") || !state.selection) return;
            if (isTypingTarget(event.target)) return;
            // A tenth of a metre by default and a whole one with Shift: fine
            // enough to close a gap, coarse enough to cross a room.
            const step = event.shiftKey ? 1 : 0.1;
            const moves: Record<string, [number, number]> = {
                ArrowLeft: [-step, 0],
                ArrowRight: [step, 0],
                ArrowUp: [0, step],
                ArrowDown: [0, -step],
            };
            const move = moves[event.key];
            if (!move || !nudgeSelection(move[0], move[1])) return;
            event.preventDefault();
            event.stopPropagation();
        },
        true,
    );

    document.addEventListener("keyup", (event) => {
        if (event.key === "`" || event.code === "Backquote") state.suspendSnap = false;
    });
    // A held key whose keyup lands on somebody else - alt-tab, a system shortcut, the browser's own find bar.
    window.addEventListener("blur", () => {
        state.suspendSnap = false;
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
        // Whatever went took its citations with it, and a pool row nothing cites any more has to go too.
        pruneUnusedReferences();
        clearSelection();
        renderSidebar();
        markDirty();
    }

    /**
 * Drop the room seeds a deletion has just orphaned.
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
 */
    /**
 * Everything on this floor that can be selected, in a stable order.
 */
    function selectableItems(): SelectionItem[] {
        const current = floor();
        const items: SelectionItem[] = [];
        for (const wall of current.walls) {
            items.push({ kind: "wall", wall });
            for (const opening of wall.openings) items.push({ kind: "opening", wall, opening });
        }
        for (const room of current.rooms) items.push({ kind: "room", room });
        for (const marker of current.markers) items.push({ kind: "marker", marker });
        return items;
    }

    /**
 * Move the selection one step, in reading order.
 */
    function stepSelection(step: number): void {
        const items = selectableItems();
        if (!items.length) return;
        const current = state.selection ? items.findIndex((item) => itemKey(item) === itemKey(state.selection as SelectionItem)) : -1;
        const next = items[(current + step + items.length * 2) % items.length] as SelectionItem;
        state.selection = next;
        state.multi = [next];
        renderSidebar();
        render();
        announceSelection();
    }

    /**
 * Say what is selected, for anyone who cannot see the highlight.
 */
    function announceSelection(): void {
        const live = document.getElementById("floorplan-live");
        if (!live) return;
        const item = state.selection;
        if (!item) {
            live.textContent = "Nothing selected";
            return;
        }
        const labels = floorLabels();
        const where = labels.get(floor()) || String(floor().level);
        // Position in the list, because the description on its own does not identify anything.
        const items = selectableItems();
        const at = items.findIndex((candidate) => itemKey(candidate) === itemKey(item)) + 1;
        const place = at > 0 ? `${at} of ${items.length}` : "";
        const what =
            item.kind === "wall"
                ? `${titleCase(item.wall.kind)} wall, ${wallLength(item.wall).toFixed(2)} metres`
                : item.kind === "opening"
                  ? titleCase(item.opening.kind)
                  : item.kind === "room"
                    ? `Room ${item.room.name || "unnamed"}`
                    : `${titleCase(item.marker.kind)} marker ${item.marker.name || ""}`.trim();
        live.textContent = `${what}, ${place}, floor ${where}`;
    }

    /**
 * Nudge whatever is selected.
 */
    function nudgeSelection(dx: number, dy: number): boolean {
        const item = state.selection;
        if (!item) return false;
        // Whether this can move at all is decided before anything is recorded.
        const roomBoundary = item.kind === "room" ? roomBoundaryWalls(item.room) : null;
        if (item.kind === "room" && (!roomBoundary || !roomBoundary.unique.length)) return false;
        checkpoint(`nudge:${itemKey(item)}`);
        if (item.kind === "wall") {
            item.wall.ax += dx;
            item.wall.ay += dy;
            item.wall.bx += dx;
            item.wall.by += dy;
        } else if (item.kind === "marker") {
            item.marker.x += dx;
            item.marker.y += dy;
        } else if (item.kind === "room") {
            const boundary = roomBoundary as NonNullable<typeof roomBoundary>;
            for (const wall of boundary.unique) {
                wall.ax += dx;
                wall.ay += dy;
                wall.bx += dx;
                wall.by += dy;
            }
            item.room.x += dx;
            item.room.y += dy;
        } else {
            // An opening lives along its wall, so a nudge slides it rather than moving it off into space.
            const length = wallLength(item.wall) || 1;
            const forward = { x: (item.wall.bx - item.wall.ax) / length, y: (item.wall.by - item.wall.ay) / length };
            const along = (dx * forward.x + dy * forward.y) / length;
            const width = item.opening.t_end - item.opening.t_start;
            const start = Math.max(0, Math.min(item.opening.t_start + along, 1 - width));
            item.opening.t_start = start;
            item.opening.t_end = start + width;
        }
        markDirty();
        announceSelection();
        return true;
    }

    function renderToolOptions(): void {
        const panel = document.getElementById("floorplan-tool-options");
        const host = document.getElementById("floorplan-tool-options-content");
        if (!panel || !host) return;
        host.replaceChildren();

        /**
 * One row of mutually exclusive choices for the armed tool.
 */
        const group = <T extends string>(
            label: string,
            options: ReadonlyArray<{ value: T; label: string; key?: string }>,
            current: T,
            onPick: (value: T) => void,
        ): void => {
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
                // Where a choice has an accelerator, it says so - the same way each tool's own tooltip names its letter.
                //
                // data-tooltip, not title: the site's tooltip is delegated from the document, so it reaches buttons built here, and a native title.
                if (option.key) {
                    button.setAttribute("data-tooltip", `${option.label} (${option.key.toUpperCase()})`);
                    button.setAttribute("data-tooltip-float", "true");
                }
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
                (Object.keys(MARKER_ICON) as MarkerKind[]).map((kind) => ({ value: kind, label: titleCase(kind), key: MARKER_KEYS[kind] })),
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
            // The toggle is the setting; the backtick is a momentary suspend for one drag.
            toggle.setAttribute("data-tooltip", "Snap to walls and angles \u00b7 hold ` to suspend for one drag");
            toggle.setAttribute("data-tooltip-float", "true");
            toggle.innerHTML = '<i class="material-symbols-outlined">grid_on</i> Snap';
            toggle.addEventListener("click", () => {
                state.snapEnabled = !state.snapEnabled;
                renderToolOptions();
            });
            wrap.appendChild(toggle);
            host.appendChild(wrap);
        }

        panel.hidden = host.childElementCount === 0;
    }

    function setTool(tool: Tool): void {
        if (state.drawing.length) commitChain();
        // A marker's popup auto-opens when it's placed and stays open across a tool switch.
        map.closePopup();
        popupOpenAtPointerDown = false;
        state.tool = tool;
        // [data-tool], not every button: the collapse toggle and the one-shot "copy this floor" action live in the same row but are not tools,.
        for (const button of document.querySelectorAll<HTMLButtonElement>("#floorplan-tools button[data-tool]")) {
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
                    ? "Click to place corners · click the first corner to close, or click the last one again to finish open-ended · Esc finishes · hold ` to ignore snapping"
                    : tool === "room"
                      ? "Click to generate a rectangular room, sized and joined from what's already drawn"
                        : tool === "marker"
                          ? "Click to drop a marker"
                          : "";
        }
        renderToolOptions();
        renderSidebar();
        // Joint and midpoint handles only ever show in select mode, and nothing else re-renders the canvas on a tool switch by itself.
        render();
    }

    // ------------------------------------------------------------- sidebar

    /**
 * Editable fields for whichever floor is showing.
 */
    function renderFloorFields(host: HTMLElement, item: Floor): void {
        const labels = floorLabels();
        const row = document.createElement("div");
        row.className = "floorplan-floor-fields";

        const code = document.createElement("input");
        code.className = "form-input floorplan-floor-fields__code";
        code.value = item.designation || "";
        code.placeholder = designationPlaceholder(item, labels);
        code.maxLength = 8;
        code.setAttribute("aria-label", "Floor number or code");
        code.addEventListener("input", () => {
            checkpoint(`floor-code:${item.uuid || item.level}`);
            item.designation = code.value.trim().slice(0, 8);
            markDirtyQuiet();
        });
        // The strip carries this storey's label and every label derived above it, so it redraws on commit rather than per keystroke.
        code.addEventListener("change", () => {
            renderFloorTabs(true);
            // Left out of that redraw, and this placeholder is the derived label,
            // which clearing the designation hands back control of.
            code.placeholder = designationPlaceholder(item, floorLabels());
        });
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
        nickname.addEventListener("change", () => renderFloorTabs(true));
        row.appendChild(nickname);

        host.appendChild(row);

        // Floor-to-ceiling, and the walking surface's height above sea level.
        const key = `floor:${item.uuid || item.level}`;
        const advancedHost = document.getElementById("floorplan-floor-advanced-fields");
        if (advancedHost) {
            advancedHost.replaceChildren();
            advancedHost.appendChild(
                metresField("Ceiling height", item.height_meters, "Metres, floor to ceiling", key, (next) => {
                    item.height_meters = next;
                }),
            );
            advancedHost.appendChild(
                metresField("Ground level", item.elevation_meters, "Metres above sea level", key, (next) => {
                    item.elevation_meters = next;
                }),
            );
        }
    }

    /**
 * Add a storey, optionally copying another floor's walls and rooms onto it.
 */
    function addFloor(where: "above" | "below", copyFrom: Floor | null): void {
        checkpoint();
        let level: number;
        if (copyFrom) {
            level = copyFrom.level + (where === "above" ? 0.5 : -0.5);
        } else {
            const levels = state.doc.floors.map((item) => item.level);
            level = where === "above" ? (levels.length ? Math.max(...levels) : -1) + 1 : (levels.length ? Math.min(...levels) : 1) - 1;
        }
        const added: Floor = { level, name: copyFrom?.name || "", walls: [], rooms: [], markers: [] };
        if (copyFrom) {
            const copied = copyFloorContents(copyFrom, { rooms: true, markers: false });
            added.walls = copied.walls;
            added.rooms = copied.rooms;
        }
        state.doc.floors.push(added);
        // Renumbers the stack and makes the new floor the active one.
        normaliseFloors(added);
        markDirty();
        renderSidebar();
        if (copyFrom) fitToContent();
    }

    /**
 * Build and show the "add a floor" dialog, resolving once it is dismissed.
 */
    function pickNewFloor(prefill: Floor | null): Promise<{ where: "above" | "below"; copyFrom: Floor | null } | null> {
        return new Promise((resolve) => {
            const dialog = document.createElement("dialog");
            dialog.className = "ul-dialog floorplan-add-floor-dialog";

            const header = document.createElement("div");
            header.className = "dialog-header";
            const heading = document.createElement("h3");
            heading.textContent = "Add a floor";
            header.appendChild(heading);

            const body = document.createElement("div");
            body.className = "ul-dialog-body";

            let where: "above" | "below" = "above";
            const aboveBtn = document.createElement("button");
            aboveBtn.type = "button";
            const belowBtn = document.createElement("button");
            belowBtn.type = "button";
            const setWhere = (next: "above" | "below") => {
                where = next;
                aboveBtn.className = `btn btn--sm${where === "above" ? " btn--primary" : " btn--ghost"}`;
                aboveBtn.setAttribute("aria-pressed", String(where === "above"));
                belowBtn.className = `btn btn--sm${where === "below" ? " btn--primary" : " btn--ghost"}`;
                belowBtn.setAttribute("aria-pressed", String(where === "below"));
            };
            aboveBtn.textContent = "Above";
            belowBtn.textContent = "Below";
            aboveBtn.addEventListener("click", () => setWhere("above"));
            belowBtn.addEventListener("click", () => setWhere("below"));
            setWhere("above");
            const posRow = document.createElement("div");
            posRow.className = "floorplan-add-floor-dialog__choice";
            posRow.append(aboveBtn, belowBtn);
            body.appendChild(field("Position", posRow));

            const labels = floorLabels();
            const floorKey = (item: Floor): string => item.uuid || String(item.level);
            const copySelect = select(
                [
                    { value: "", label: "Nothing - start blank" },
                    ...state.doc.floors.map((item) => ({
                        value: floorKey(item),
                        label: `${labels.get(item) || item.level}${item.name ? ` – ${item.name}` : ""}`,
                    })),
                ],
                prefill ? floorKey(prefill) : "",
                () => {},
            );
            body.appendChild(field("Copy layout from", copySelect));

            const footer = document.createElement("div");
            footer.className = "dialog-footer";
            const cancelBtn = document.createElement("button");
            cancelBtn.type = "button";
            cancelBtn.className = "btn btn--ghost";
            cancelBtn.textContent = "Cancel";
            const addBtn = document.createElement("button");
            addBtn.type = "button";
            addBtn.className = "btn btn--primary";
            addBtn.textContent = "Add floor";
            footer.append(cancelBtn, addBtn);

            dialog.append(header, body, footer);
            document.body.appendChild(dialog);

            const cleanup = (result: { where: "above" | "below"; copyFrom: Floor | null } | null) => {
                dialog.close();
                dialog.remove();
                resolve(result);
            };
            cancelBtn.addEventListener("click", () => cleanup(null));
            addBtn.addEventListener("click", () => {
                const copyFrom = copySelect.value ? state.doc.floors.find((item) => floorKey(item) === copySelect.value) || null : null;
                cleanup({ where, copyFrom });
            });
            dialog.addEventListener("cancel", () => cleanup(null));

            dialog.showModal();
        });
    }

    /** Open the add-floor dialog and act on its result, if not cancelled. */
    async function promptAddFloor(prefill: Floor | null): Promise<void> {
        const choice = await pickNewFloor(prefill);
        if (choice) addFloor(choice.where, choice.copyFrom);
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
        // A whole storey's worth of citations just went.
        pruneUnusedReferences();
        state.floorIndex = Math.min(state.floorIndex, state.doc.floors.length - 1);
        // Otherwise the stack reads "1, 2, 4": the storey above a deleted one keeps a level nothing sits below any more, and "the floor below".
        normaliseFloors(state.doc.floors[state.floorIndex] as Floor | undefined || null);
        clearSelection();
        renderSidebar();
        markDirty();
    }

    /**
 * Keep the stack ordered by level, contiguous, and pointing at the same storey it was before.
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
 * Ghost text for the blank floor-code input.
 */
    function designationPlaceholder(item: Floor, labels: Map<Floor, string>): string {
        const label = labels.get(item) || "";
        return label === GROUND_LABEL ? "1" : label;
    }

    /**
 * Redraw the floor strip, and the current floor's fields beneath it.
 */
    function renderFloorTabs(keepFields = false): void {
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
            // Only the floor you are on offers to delete itself.
            if (state.doc.floors.length > 1 && index === state.floorIndex) {
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
        // One button rather than the previous above/below/duplicate three.
        const add = document.createElement("button");
        add.type = "button";
        add.className = "btn btn--sm btn--ghost floorplan-floor-tab__add";
        add.innerHTML = '<i class="material-symbols-outlined">add</i>';
        add.setAttribute("aria-label", "Add floor");
        add.setAttribute("data-tooltip", "Add floor");
        add.setAttribute("data-tooltip-pos", "right");
        add.addEventListener("click", () => void promptAddFloor(null));
        host.appendChild(add);

        // Left alone when the redraw was asked for by one of these fields.
        const fieldsHost = keepFields ? null : document.getElementById("floorplan-floor-fields");
        if (fieldsHost) {
            fieldsHost.replaceChildren();
            renderFloorFields(fieldsHost, floor());
        }
    }

    function field(labelText: string, input: HTMLElement): HTMLLabelElement {
        const label = document.createElement("label");
        label.className = "floorplan-field";
        const span = document.createElement("span");
        span.textContent = labelText;
        label.append(span, input);
        return label;
    }

    /**
 * A labelled metres input.
 */
    function metresField(label: string, value: number | null | undefined, placeholder: string, key: string, apply: (next: number | null) => void): HTMLLabelElement {
        const input = document.createElement("input");
        input.type = "number";
        input.className = "form-input";
        input.min = "0";
        input.step = "0.05";
        input.value = value === null || value === undefined ? "" : String(value);
        input.placeholder = placeholder;
        input.setAttribute("aria-label", label);
        input.addEventListener("input", () => {
            checkpoint(`${key}:${label}`);
            const parsed = Number.parseFloat(input.value);
            apply(input.value.trim() === "" || !Number.isFinite(parsed) ? null : parsed);
            markDirtyQuiet();
        });
        return field(label, input);
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
 */
    /**
 * The pin's photos, offered as attachments for one item.
 */
    /**
 * Every reference-pool uuid some item on the plan still cites.
 */
    function citedReferences(): Set<string> {
        const cited = new Set<string>();
        const take = (details: ItemDetails): void => {
            for (const uuid of details.references ?? []) cited.add(uuid);
        };
        for (const floorItem of state.doc.floors) {
            for (const wall of floorItem.walls) {
                take(wall);
                for (const opening of wall.openings) {
                    take(opening);
                    for (const lock of opening.locks ?? []) take(lock);
                }
            }
            for (const room of floorItem.rooms) take(room);
            for (const marker of floorItem.markers) take(marker);
        }
        return cited;
    }

    /** Drop pool rows nothing cites, so the pool cannot silt up. */
    function pruneUnusedReferences(): void {
        const pool = state.doc.reference_pool;
        if (!pool?.length) return;
        const cited = citedReferences();
        state.doc.reference_pool = pool.filter((entry) => !entry.uuid || cited.has(entry.uuid));
    }

    function renderReferences(host: HTMLElement, item: ItemDetails): void {
        const pool = (state.doc.reference_pool ??= []);
        const citedRows = (item.references ?? []).map((uuid) => pool.find((entry) => entry.uuid === uuid)).filter((entry): entry is Reference => Boolean(entry));
        // A citation whose photo is gone: the image was deleted from the owner's media, and the reference deliberately survived it.
        const orphans = citedRows.filter((entry) => !entry.image_uuid || !pinPhotos.some((photo) => photo.uuid === entry.image_uuid));
        if (!pinPhotos.length && !orphans.length) return;
        const cited = new Set(item.references ?? []);
        /** The pool row standing for one image, if the plan has one. */
        const rowFor = (imageUuid: string): Reference | undefined => pool.find((entry) => entry.image_uuid === imageUuid);

        const wrap = document.createElement("div");
        wrap.className = "floorplan-photos";
        const title = document.createElement("span");
        title.className = "floorplan-field__label";
        title.textContent = "Photos";
        wrap.appendChild(title);

        const strip = document.createElement("div");
        strip.className = "floorplan-photos__strip";
        for (const photo of pinPhotos) {
            const row = rowFor(photo.uuid);
            const attached = Boolean(row?.uuid && cited.has(row.uuid));
            const button = document.createElement("button");
            button.type = "button";
            button.className = `floorplan-photo${attached ? " is-attached" : ""}`;
            button.setAttribute("aria-pressed", attached ? "true" : "false");
            button.setAttribute("aria-label", photo.caption || "Photo");
            button.setAttribute("data-tooltip", photo.caption || "Photo");
            button.setAttribute("data-tooltip-float", "true");
            const image = document.createElement("img");
            image.src = photo.url;
            image.alt = "";
            image.loading = "lazy";
            button.appendChild(image);
            button.addEventListener("click", () => {
                checkpoint();
                const existing = rowFor(photo.uuid);
                if (attached && existing?.uuid) {
                    item.references = (item.references ?? []).filter((uuid) => uuid !== existing.uuid);
                    // A pool row nothing cites any more goes with the last citation.
                    pruneUnusedReferences();
                } else {
                    const target = existing ?? { uuid: nextLocalId(), kind: "photo", title: photo.caption || "", image_uuid: photo.uuid };
                    if (!existing) pool.push(target);
                    item.references = [...(item.references ?? []), target.uuid as string];
                }
                renderSidebar();
                markDirty();
            });
            strip.appendChild(button);
        }
        if (pinPhotos.length) wrap.appendChild(strip);

        for (const orphan of orphans) {
            const chip = document.createElement("div");
            chip.className = "floorplan-photo-missing";
            const label = document.createElement("span");
            // Whatever the reference still knows about the picture it stood for.
            label.textContent = orphan.title || orphan.url || "Photo no longer available";
            chip.appendChild(label);
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "btn btn--icon-sm";
            remove.setAttribute("aria-label", `Remove ${orphan.title || "missing photo"}`);
            remove.innerHTML = '<i class="material-symbols-outlined">close</i>';
            remove.addEventListener("click", () => {
                checkpoint();
                item.references = (item.references ?? []).filter((uuid) => uuid !== orphan.uuid);
                pruneUnusedReferences();
                renderSidebar();
                markDirty();
            });
            chip.appendChild(remove);
            wrap.appendChild(chip);
        }

        host.appendChild(wrap);
    }

    function renderItemDetails(host: HTMLElement, item: ItemDetails, key: string): void {
        const filled = Boolean(item.description || item.condition || attribute(item, "material") || item.built_date || item.references?.length);
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

        // Stored on every item and never asked for until now.
        const built = document.createElement("input");
        built.type = "date";
        built.className = "form-input";
        built.value = item.built_date || "";
        built.addEventListener("input", () => {
            checkpoint(`${key}:built`);
            item.built_date = built.value || null;
            markDirtyQuiet();
        });
        box.appendChild(field("Built", built));

        renderReferences(box, item);

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
 */
    function renderMarkerAppearance(marker: Marker | null): void {
        const host = markerAppearance;
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

        // The site's shared colour picker, same as the labels and pin dialogs use, rather than a set of swatches of this editor's own.
        const colourInput = document.getElementById("color-value-floorplan-marker") as HTMLInputElement | null;
        if (colourInput) {
            const current = marker.color || "";
            colourInput.value = current;
            for (const swatch of host.querySelectorAll<HTMLButtonElement>(".color-swatch")) {
                swatch.classList.toggle("selected", (swatch.dataset.color || "") === current);
            }
            colourInput.onchange = () => {
                checkpoint();
                marker.color = colourInput.value || null;
                markDirty();
            };
        }
    }

    /**
 * The icon and colour pickers, server-rendered once and shown per marker.
 */
    const markerAppearance = document.getElementById("floorplan-marker-appearance");
    /** Where it lives when no marker is selected. */
    const markerAppearanceHome = markerAppearance?.parentElement ?? null;

    function renderSidebar(): void {
        const host = document.getElementById("floorplan-form");
        if (!host) return;
        // Home before the clear: without this the pickers are among the
        // children being replaced, and leave the document with them.
        if (markerAppearance && markerAppearanceHome) markerAppearanceHome.appendChild(markerAppearance);
        host.replaceChildren();
        const selection = state.selection;
        renderMarkerAppearance(selection && selection.kind === "marker" && state.multi.length === 1 ? selection.marker : null);
        if (!selection) return;

        // More than one item selected: a per-kind edit form doesn't apply, so offer only what makes sense in bulk.
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
                    checkpoint();
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
                checkpoint();
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
            // What it looks like, beside what it is.
            if (markerAppearance) host.appendChild(markerAppearance);
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
                        // The swing question only applies to some kinds, so the
                        // form has to be rebuilt when the answer changes.
                        renderSidebar();
                        markDirty();
                    }),
                ),
            );
            // Only where it means something: a doorway is the hole with no door
            // in it, and a window has nothing that sweeps across the floor.
            if (swings(opening.kind)) {
                host.appendChild(
                    field(
                        "Swing",
                        select(OPENING_SWINGS, opening.swing, (v) => {
                            opening.swing = v as Opening["swing"];
                            markDirty();
                        }),
                    ),
                );
            }
            // How high its bottom edge sits above the floor.
            host.appendChild(
                metresField("Sill height", opening.sill_meters, "Metres above the floor", `opening:${opening.uuid}`, (next) => {
                    opening.sill_meters = next;
                }),
            );

            renderLockControls(host, opening);
        }

        // Not for a room: a room seed is a name attached to a region, so deleting it removes the name and leaves every wall standing, which.
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
 */
    /**
 * A room's boundary walls, split into the room's own and the rest.
 */
    function roomBoundaryWalls(room: RoomSeed): RoomBoundary | null {
        const face = faceForSeed({ x: room.x, y: room.y }, state.faces);
        if (!face) return null;
        return splitRoomBoundary(face, floor().walls);
    }

    /**
 * Every already-bound room on this floor, one entry per occupied face.
 */
    function occupiedFaces(current: Floor, faces: readonly Face[]): Array<{ room: RoomSeed; face: Face }> {
        const seen = new Set<Face>();
        const result: Array<{ room: RoomSeed; face: Face }> = [];
        for (const room of current.rooms) {
            const face = faceForSeed({ x: room.x, y: room.y }, faces);
            if (face && !seen.has(face)) {
                seen.add(face);
                result.push({ room, face });
            }
        }
        return result;
    }

    /**
 * Nudge each point of a ring slightly toward its own centroid.
 */
    function eroded(ring: readonly Pt[]): Pt[] {
        const EROSION_METERS = 0.05;
        const cx = ring.reduce((sum, p) => sum + p.x, 0) / ring.length;
        const cy = ring.reduce((sum, p) => sum + p.y, 0) / ring.length;
        return ring.map((p) => {
            const dx = cx - p.x;
            const dy = cy - p.y;
            const len = Math.hypot(dx, dy);
            if (len < 1e-9) return p;
            const shrink = Math.min(EROSION_METERS, len * 0.25);
            return { x: p.x + (dx / len) * shrink, y: p.y + (dy / len) * shrink };
        });
    }

    /** Whether a simple polygon (a room rectangle, a face's own ring) genuinely overlaps a face - not merely touches its boundary. */
    function polygonOverlapsFace(corners: readonly Pt[], face: Face): boolean {
        if (eroded(corners).some((p) => pointInRing(p, face.ring))) return true;
        if (eroded(face.ring).some((p) => pointInRing(p, corners))) return true;
        return false;
    }

    /**
 * Split off this room's own copy of any wall it merely shares with a neighbouring room, so a move drags only the moving room's geometry.
 */
    function detachSharedWalls(current: Floor, boundary: RoomBoundary, faces: readonly Face[]): Wall[] {
        const neighbours = occupiedFaces(current, faces)
            .filter((entry) => entry.face !== boundary.face)
            .map((entry) => entry.face);
        return boundary.unique.map((wall) => {
            const id = wallId(wall);
            if (!neighbours.some((face) => face.wallIds.includes(id))) return wall;
            const clone: Wall = { ...wall, uuid: nextLocalId(), openings: wall.openings.map((opening) => ({ ...opening, uuid: nextLocalId() })) };
            current.walls.push(clone);
            return clone;
        });
    }

    /**
 * Offer to delete a whole room at once - its seed and the walls that are only ever this room's - rather than one wall at a time.
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

    /**
 * The locks fitted to one opening.
 */
    function renderLockControls(host: HTMLElement, opening: Opening): void {
        // A window does not have a lock worth recording for getting in, and a doorway is the hole where a door used to be.
        if (!swings(opening.kind) && opening.kind !== "hatch") return;
        const wrap = document.createElement("div");
        wrap.className = "floorplan-locks";
        const title = document.createElement("span");
        title.className = "floorplan-field__label";
        title.textContent = "Locks";
        wrap.appendChild(title);

        const locks = opening.locks ?? [];
        locks.forEach((lock, index) => {
            const row = document.createElement("div");
            row.className = "floorplan-lock";

            const name = document.createElement("input");
            name.className = "form-input";
            name.value = lock.name || "";
            name.placeholder = "Padlock, deadbolt, chain";
            name.setAttribute("aria-label", "What kind of lock");
            name.addEventListener("input", () => {
                checkpoint(`lock-name:${lock.uuid || index}`);
                lock.name = name.value;
                markDirtyQuiet();
            });
            row.appendChild(name);

            row.appendChild(
                select(LOCK_STATES, lock.state, (value) => {
                    lock.state = value as Lock["state"];
                    markDirty();
                }),
            );

            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "btn btn--icon-sm floorplan-lock__remove";
            remove.innerHTML = '<i class="material-symbols-outlined">close</i>';
            remove.setAttribute("aria-label", `Remove ${lock.name || "this lock"}`);
            remove.addEventListener("click", () => {
                checkpoint();
                opening.locks = (opening.locks ?? []).filter((item) => item !== lock);
                renderSidebar();
                markDirty();
            });
            row.appendChild(remove);

            // What opens it, in whatever shape the recorder used.
            const key = document.createElement("input");
            key.className = "form-input floorplan-lock__key";
            key.value = String((lock.key_attributes as Record<string, unknown> | undefined)?.note ?? "");
            key.placeholder = "What opens it";
            key.setAttribute("aria-label", "What opens this lock");
            key.addEventListener("input", () => {
                checkpoint(`lock-key:${lock.uuid || index}`);
                const note = key.value.trim();
                const rest = { ...(lock.key_attributes ?? {}) };
                if (note) rest.note = note;
                else delete rest.note;
                lock.key_attributes = rest;
                markDirtyQuiet();
            });

            const entry = document.createElement("div");
            entry.className = "floorplan-lock-entry";
            entry.appendChild(row);
            entry.appendChild(key);
            // A lock is a floorplan item like any other, so it gets the same description/condition/material block everything else does.
            renderItemDetails(entry, lock, `lock:${lock.uuid || index}`);
            wrap.appendChild(entry);
        });

        const add = document.createElement("button");
        add.type = "button";
        add.className = "btn btn--sm btn--ghost";
        add.textContent = locks.length ? "+ Another lock" : "+ Lock";
        add.addEventListener("click", () => {
            checkpoint();
            opening.locks = [...(opening.locks ?? []), { uuid: nextLocalId(), name: "", state: "unknown" }];
            renderSidebar();
            markDirty();
        });
        wrap.appendChild(add);
        host.appendChild(wrap);
    }

    function renderConnectorControls(host: HTMLElement, marker: Marker): void {
        const current = floor();
        const candidates = connectorCandidates(state.doc.floors, current, marker);

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
                checkpoint();
                marker.connector_id = null;
                renderSidebar();
                markDirty();
            });
            wrap.appendChild(unlink);
        } else if (!candidates.length) {
            const none = document.createElement("p");
            none.className = "floorplan-hint";
            none.textContent = "Add a stair or lift on another floor to link them.";
            wrap.appendChild(none);
        } else {
            // Nearest storey first, so in the ordinary case the right one is the first button.
            const NEAR = 4;
            const shown = state.connectorsExpanded ? candidates : candidates.slice(0, NEAR);
            for (const candidate of shown) {
                const button = document.createElement("button");
                button.type = "button";
                button.className = "btn btn--sm btn--ghost";
                const where = candidate.floor.name || floorLabels().get(candidate.floor) || `Level ${candidate.floor.level}`;
                button.textContent = `Link to ${candidate.marker.name || candidate.marker.kind} on ${where}`;
                button.addEventListener("click", () => {
                    checkpoint();
                    // Adopt the counterpart's id when it already has one, so a third floor joins the same shaft rather than starting a parallel one.
                    const shared = candidate.marker.connector_id || newConnectorId();
                    candidate.marker.connector_id = shared;
                    marker.connector_id = shared;
                    renderSidebar();
                    markDirty();
                });
                wrap.appendChild(button);
            }
            if (candidates.length > shown.length) {
                const more = document.createElement("button");
                more.type = "button";
                more.className = "btn btn--sm btn--ghost";
                more.textContent = `Show ${candidates.length - shown.length} more`;
                more.addEventListener("click", () => {
                    state.connectorsExpanded = true;
                    renderSidebar();
                });
                wrap.appendChild(more);
            }
        }
        host.appendChild(wrap);
    }

    /**
 * Square the drawing axis to a wall the user picks.
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

    /**
 * Whether this document is something other than the viewer's own saved work.
 */
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
            // An unnamed version is labelled by the thing that actually tells it apart from its siblings - the date it came into force.
            const shown = version.name || version.valid_from || "Original";
            button.textContent = isCurrent ? `${shown} (current)` : shown;
            button.setAttribute("data-tooltip", version.valid_from ? `In force from ${version.valid_from}` : "The original baseline");
            button.setAttribute("data-tooltip-float", "true");
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
        // Restores a saved plan already turned to face its building.
        if (canRotateView) map.setBearing(state.doc.rotation_degrees || 0);
        showPlanFields();
        // A brand-new plan starts from the real footprint when one is known, so
        // the first thing on screen is the building rather than a blank map.
        const fresh = !state.loadFailed && state.doc.floors.length === 1 && !(state.doc.floors[0] as Floor).walls.length;
        if (fresh && seedFromOutline(state.doc.floors[0] as Floor)) state.dirty = true;
        else state.dirty = false;
        // A switch away from the version that was selected/mid-drag leaves stale references into a document that no longer exists.
        normaliseFloors(state.doc.floors[0] as Floor | undefined || null);
        clearSelection();
        // An undo snapshot outliving the document it was taken from is not a safety net.
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
                // Neither branch above matched, so state.doc is still the blank document state was initialised with.
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
        // Nothing prompts before switching - autosave already means the user never explicitly asked to "save", so flush whatever is pending.
        if (state.dirty) await save(false);
        // A save for the version being left could still be in flight.
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

    /**
 * Persist the current document.
 */
    async function save(asNewVersion = false): Promise<void> {
        // Two overlapping saves would each overwrite state.doc.uuid from their own response when they resolve, regardless of which request was.
        await waitForSaveSlot();
        // Both fields are already on state.doc, written there as they were typed.
        for (const item of state.doc.floors) {
            for (const marker of item.markers) {
                const world = toLatLng({ x: marker.x, y: marker.y });
                marker.lat = world[0];
                marker.lng = world[1];
            }
        }
        const payload: FloorplanDocument = { ...state.doc };
        // Dropping the uuid is what makes the save fork a new dated version instead of overwriting the one that was loaded.
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
                // The save view always answers errors as {ok, error} JSON.
                let message = `Could not save this floorplan. (${response.status})`;
                try {
                    const body = (await response.json()) as { error?: string };
                    if (body.error) message = body.error;
                } catch {
                    // Not JSON - keep the generic, status-coded message above.
                }
                if (response.status === 409) {
                    // Not retried and not backed off: the other tab is not going to un-save, so every attempt from here would either fail the same way.
                    state.superseded = true;
                    toast.warning(message);
                    updateSaveStatus();
                    return;
                }
                toast.warning(message);
                noteSaveFailure();
                return;
            }
            // The save view answers {ok, floorplan: <document>}.
            const body = (await response.json()) as { ok?: boolean; floorplan?: FloorplanDocument };
            if (body.floorplan) {
                state.doc.uuid = body.floorplan.uuid;
                applyServerIds(sent, body.floorplan);
                // The response reports where the saved row actually lives, so a document loaded as someone else's community plan becomes "local" here.
                state.doc.origin = body.floorplan.origin;
                state.versions = body.floorplan.versions || [];
            }
            state.doc.version_token = body.floorplan?.version_token;
            state.dirty = false;
            saveFailed = false;
            retryAttempt = 0;
            updateMoreMenu();
            renderOriginBanner();
            renderVersions();
            // Autosave stays quiet - a toast for every keystroke-driven save would be constant noise.
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

    // Written through on edit like every other field, rather than read out of the DOM at save time.
    for (const id of ["floorplan-name", "floorplan-valid-from"]) {
        document.getElementById(id)?.addEventListener("input", () => {
            checkpoint(`plan-${id}`);
            readPlanFields();
            markDirtyQuiet();
        });
    }

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
            // Shares the same in-flight flag as save() - both so this can't overlap an autosave (the same race a concurrent save() call could hit),.
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
    // A one-shot action beside the drawing tools, not one of them - it does
    // not arm a mode, so it carries no [data-tool] and is wired on its own.
    document.getElementById("floorplan-copy-floor")?.addEventListener("click", () => void promptAddFloor(floor()));
    // Kept working for anything that still renders one; the toolbar's own
    // marker-kind buttons now live in the tool options panel.
    for (const button of document.querySelectorAll<HTMLButtonElement>("[data-marker-kind]")) {
        button.addEventListener("click", () => {
            state.markerKind = button.dataset.markerKind as MarkerKind;
            setTool("marker");
        });
    }
    document.getElementById("floorplan-delete")?.addEventListener("click", () => deleteSelection());

    document.getElementById("floorplan-start-outline")?.addEventListener("click", () => {
        checkpoint();
        if (seedFromOutline(floor())) markDirty();
        else toast.info("No building outline is known for this place yet.");
    });
    document.getElementById("floorplan-start-rectangle")?.addEventListener("click", () => {
        // Four exterior walls around the current view's middle third.
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
