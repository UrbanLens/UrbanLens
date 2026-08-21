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
import { PlanProjection, type Pt, distance, projectOnSegment } from "../shared/floorplan/coords";
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
    nextLocalId,
    wallLength,
    wallSegments,
} from "../shared/floorplan/document";
import { type Face, deriveFaces, faceForSeed } from "../shared/floorplan/planar";
import { PIXEL_TOLERANCES, snapPoint } from "../shared/floorplan/snapping";
import { createMapImageOverlays, type MapOverlayEntry } from "../shared/map-image-overlays";
import { createMapLayers } from "../shared/map-layers";

declare const L: typeof import("leaflet");

type Tool = "select" | "wall" | "marker";

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
    const kind = document.createElement("p");
    kind.className = "floorplan-marker-popup__kind";
    kind.textContent = marker.kind;
    wrap.appendChild(kind);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "btn btn--sm btn--danger floorplan-marker-popup__delete";
    remove.textContent = "Delete";
    wrap.appendChild(remove);
    return wrap;
}

function boot(): void {
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
    const map = L.map("floorplan-map", { zoomControl: true, doubleClickZoom: false, attributionControl: false, boxZoom: false }).setView([lat, lng], 20);

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
        dirty: false,
        suspendSnap: false,
        faces: [] as Face[],
        versions: [] as VersionSummary[],
        showUnderlay: false,
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
    const overlays = readJson<MapOverlayEntry[]>("floorplan-overlays") || [];
    if (overlays.length) {
        const control = createMapImageOverlays(L, map, { cornersUrl: () => "", csrfToken: getCsrfToken() });
        control.sync(overlays.map((entry) => ({ ...entry, locked: true })));
    }

    let projection = new PlanProjection({ lat, lng });
    const wallLayer = L.layerGroup().addTo(map);
    const roomLayer = L.layerGroup().addTo(map);
    const markerLayer = L.layerGroup().addTo(map);
    const handleLayer = L.layerGroup().addTo(map);
    const ghostLayer = L.layerGroup().addTo(map);
    // Added first so it always paints beneath the live floor.
    const underlayLayer = L.layerGroup().addTo(map);

    const toLatLng = (p: Pt): [number, number] => {
        const world = projection.toWorld(p);
        return [world.lat, world.lng];
    };
    const toLocal = (latlng: L.LatLng): Pt => projection.toLocal({ lat: latlng.lat, lng: latlng.lng });

    /** Snap tolerances in metres, derived from the fixed pixel tolerances. */
    function tolerances(): { endpoint: number; wall: number; extension: number } {
        const mpp = metresPerPixel();
        return {
            endpoint: PIXEL_TOLERANCES.endpoint * mpp,
            wall: PIXEL_TOLERANCES.wall * mpp,
            extension: PIXEL_TOLERANCES.extension * mpp,
        };
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
        if (!floors.length) floors.push({ level: 0, name: "Ground floor", walls: [], rooms: [], markers: [] });
        state.floorIndex = Math.min(state.floorIndex, floors.length - 1);
        return floors[state.floorIndex] as Floor;
    };

    function markDirty(): void {
        state.dirty = true;
        render();
    }

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
    }

    // ---------------------------------------------------------------- render

    function render(): void {
        wallLayer.clearLayers();
        roomLayer.clearLayers();
        markerLayer.clearLayers();
        handleLayer.clearLayers();

        const current = floor();
        const segments = wallSegments(current);
        const derived = deriveFaces(segments);
        state.faces = derived.faces;

        // Once there's an exterior to read as "the building", the basemap
        // recedes (desaturated, in the tile pane only - an image overlay
        // renders in Leaflet's separate overlayPane, so a traced blueprint
        // stays crisp) and the plan itself becomes the thing in focus.
        mapEl.classList.toggle("has-plan", current.walls.some((wall) => wall.kind === "exterior"));

        // Rooms first so walls draw on top of their fills.
        for (const face of derived.faces) {
            const seed = current.rooms.find((room) => faceForSeed({ x: room.x, y: room.y }, [face]) === face);
            const roomSelected = seed ? isSelected({ kind: "room", room: seed }) : false;
            const polygon = L.polygon(face.ring.map(toLatLng), {
                className: "floorplan-room",
                ...(seed ? ROOM_FILL : UNBOUND_FILL),
                ...(roomSelected ? { color: "#00838f", weight: 3 } : {}),
            }).addTo(roomLayer);
            const label = seed ? seed.name || "Unnamed" : "Unnamed room";
            // Permanent, not on hover: a room appearing and naming its own area
            // the instant a loop closes is what teaches the wall-first model,
            // and a badge nobody sees teaches nothing.
            polygon.bindTooltip(`${label} · ${face.area.toFixed(1)} m²`, {
                direction: "center",
                className: "floorplan-room-label",
                permanent: true,
            });
            polygon.on("click", (event) => {
                // Checked before stopping propagation: a room fill covers a
                // large area, and a stop here regardless of tool silently
                // swallowed every wall/marker click landing inside a room -
                // exactly where someone is likeliest to want to add one.
                if (state.tool !== "select") return;
                L.DomEvent.stop(event);
                const bound = seed || addSeedAt(centroid(face.ring));
                selectItem({ kind: "room", room: bound }, event);
            });
            polygon.on("contextmenu", (event) => {
                if (state.tool !== "select") return;
                const bound = seed || addSeedAt(centroid(face.ring));
                showContextMenu(event, { kind: "room", room: bound });
            });
        }

        // Seeds that bind to no face - the "not enclosed" state.
        for (const room of current.rooms) {
            if (faceForSeed({ x: room.x, y: room.y }, derived.faces)) continue;
            L.circleMarker(toLatLng({ x: room.x, y: room.y }), { radius: 5, color: "#ef6c00", fillOpacity: 1 })
                .bindTooltip(`${room.name || "Room"} — not enclosed`, { direction: "top" })
                .addTo(roomLayer);
        }

        for (const wall of current.walls) {
            const style = WALL_STYLE[wall.kind] || WALL_STYLE.interior;
            const selected = isSelected({ kind: "wall", wall });
            const line = L.polyline([toLatLng({ x: wall.ax, y: wall.ay }), toLatLng({ x: wall.bx, y: wall.by })], {
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
            renderOpenings(wall, selected);
            // Only one wall's handles are ever worth drawing: with several
            // walls multi-selected there is no single "the" endpoint drag to
            // offer, and drawing all of them invites dragging the wrong one.
            if (selected && state.multi.length === 1) renderWallHandles(wall);
        }

        for (const marker of current.markers) {
            const selected = isSelected({ kind: "marker", marker });
            const node = L.marker(toLatLng({ x: marker.x, y: marker.y }), { icon: markerIcon(marker, selected), draggable: state.tool === "select" }).addTo(markerLayer);
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
            node.on("dragend", () => {
                const p = toLocal(node.getLatLng());
                marker.x = p.x;
                marker.y = p.y;
                markDirty();
            });
        }

        // Healed joins, so a bridged near-miss is visible rather than magic.
        for (const join of derived.healed) {
            L.circleMarker(toLatLng(join.at), { radius: 3, color: "#7e57c2", fillOpacity: 0.9, weight: 1 })
                .bindTooltip(`Gap of ${join.gap.toFixed(2)} m closed automatically`, { direction: "top" })
                .addTo(handleLayer);
        }

        renderUnderlay();
        renderFloorTabs();
        updateEmptyState(current);
    }

    function renderOpenings(wall: Wall, selected: boolean): void {
        const a = { x: wall.ax, y: wall.ay };
        const b = { x: wall.bx, y: wall.by };
        const at = (t: number): Pt => ({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t });
        for (const opening of wall.openings) {
            const openingSelected = isSelected({ kind: "opening", wall, opening });
            const colour = opening.kind === "window" ? "#1e88e5" : "#fb8c00";
            const line = L.polyline([toLatLng(at(opening.t_start)), toLatLng(at(opening.t_end))], {
                className: "floorplan-opening",
                color: openingSelected ? "#00838f" : colour,
                weight: openingSelected ? 9 : 6,
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
            if (selected) renderOpeningHandles(wall, opening, at);
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
            let dragging = false;
            handle.on("mousedown", () => {
                dragging = true;
                map.dragging.disable();
            });
            map.on("mousemove", (event: L.LeafletMouseEvent) => {
                if (!dragging) return;
                const along = projectOnSegment(toLocal(event.latlng), { x: wall.ax, y: wall.ay }, { x: wall.bx, y: wall.by }).t;
                if (end === "t_start") opening.t_start = Math.min(along, opening.t_end - MIN_WIDTH);
                else opening.t_end = Math.max(along, opening.t_start + MIN_WIDTH);
                opening.t_start = Math.max(0, opening.t_start);
                opening.t_end = Math.min(1, opening.t_end);
                render();
            });
            map.on("mouseup", () => {
                if (!dragging) return;
                dragging = false;
                map.dragging.enable();
                markDirty();
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
        if (!state.showUnderlay) return;
        const current = floor();
        const below = state.doc.floors.filter((item) => item.level < current.level).sort((x, y) => y.level - x.level)[0];
        if (!below) return;
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

    function renderWallHandles(wall: Wall): void {
        for (const end of ["a", "b"] as const) {
            const point = end === "a" ? { x: wall.ax, y: wall.ay } : { x: wall.bx, y: wall.by };
            const handle = L.circleMarker(toLatLng(point), { radius: 6, color: "#00838f", fillColor: "#fff", fillOpacity: 1, className: "floorplan-handle" }).addTo(handleLayer);
            let dragging = false;
            handle.on("mousedown", () => {
                dragging = true;
                map.dragging.disable();
            });
            map.on("mousemove", (event: L.LeafletMouseEvent) => {
                if (!dragging) return;
                const raw = toLocal(event.latlng);
                const others = wallSegments(floor()).filter((s) => s.wallId !== wall.uuid);
                const snapped = snapPoint(raw, others, tolerances(), { suspended: state.suspendSnap });
                if (end === "a") {
                    wall.ax = snapped.point.x;
                    wall.ay = snapped.point.y;
                } else {
                    wall.bx = snapped.point.x;
                    wall.by = snapped.point.y;
                }
                render();
            });
            map.on("mouseup", () => {
                if (!dragging) return;
                dragging = false;
                map.dragging.enable();
                markDirty();
            });
        }
    }

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
        if (state.tool !== "select" || event.button !== 0 || !event.shiftKey) return;
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
            addAction("Add opening", () => {
                item.wall.openings.push({ uuid: nextLocalId(), kind: "door", t_start: 0.45, t_end: 0.55, swing: "none" });
                renderSidebar();
                markDirty();
            });
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

    function centroid(ring: readonly Pt[]): Pt {
        let x = 0;
        let y = 0;
        for (const p of ring) {
            x += p.x;
            y += p.y;
        }
        return { x: x / ring.length, y: y / ring.length };
    }

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

    function addSeedAt(point: Pt): RoomSeed {
        const seed: RoomSeed = { uuid: nextLocalId(), name: "", x: point.x, y: point.y };
        floor().rooms.push(seed);
        state.dirty = true;
        return seed;
    }

    // ------------------------------------------------------------- drawing

    function commitChain(): void {
        const points = state.drawing;
        if (points.length >= 2) {
            for (let i = 0; i < points.length - 1; i++) {
                const a = points[i] as Pt;
                const b = points[i + 1] as Pt;
                if (distance(a, b) < 1e-6) continue;
                floor().walls.push({
                    uuid: nextLocalId(),
                    kind: floor().walls.length === 0 ? "exterior" : "interior",
                    thickness: "normal",
                    ax: a.x,
                    ay: a.y,
                    bx: b.x,
                    by: b.y,
                    openings: [],
                });
            }
            state.dirty = true;
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
            suspended: state.suspendSnap,
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
        const raw = toLocal(event.latlng);
        if (state.tool === "wall") {
            const from = state.drawing.length ? (state.drawing[state.drawing.length - 1] as Pt) : null;
            const snapped = snapPoint(raw, wallSegments(floor()), tolerances(), {
                from,
                suspended: state.suspendSnap,
                axisRadians: (state.doc.rotation_degrees * Math.PI) / 180,
            });
            // Clicking the chain's own origin closes the loop and finishes.
            const origin = state.drawing[0];
            if (origin && state.drawing.length >= 2 && distance(snapped.point, origin) < 12 * metresPerPixel()) {
                state.drawing.push(origin);
                commitChain();
                return;
            }
            state.drawing.push(snapped.point);
            drawGhost();
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

    document.addEventListener("keydown", (event) => {
        if (event.key === "Alt") state.suspendSnap = true;
        if (event.key === "Escape") {
            closeContextMenu();
            if (state.drawing.length) commitChain();
            else {
                clearSelection();
                renderSidebar();
                render();
            }
        }
        if ((event.key === "Delete" || event.key === "Backspace") && state.selection) {
            const target = event.target as HTMLElement | null;
            if (target && ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return;
            event.preventDefault();
            deleteSelection();
        }
        if (event.key === "1") setTool("select");
        if (event.key === "2") setTool("wall");
        if (event.key === "3") setTool("marker");
    });
    document.addEventListener("keyup", (event) => {
        if (event.key === "Alt") state.suspendSnap = false;
    });

    function deleteSelection(): void {
        const current = floor();
        const targets: SelectionItem[] = state.multi.length ? state.multi : state.selection ? [state.selection] : [];
        if (!targets.length) return;
        const walls = new Set(targets.filter((t): t is Extract<SelectionItem, { kind: "wall" }> => t.kind === "wall").map((t) => t.wall));
        const rooms = new Set(targets.filter((t): t is Extract<SelectionItem, { kind: "room" }> => t.kind === "room").map((t) => t.room));
        const markers = new Set(targets.filter((t): t is Extract<SelectionItem, { kind: "marker" }> => t.kind === "marker").map((t) => t.marker));
        if (walls.size) current.walls = current.walls.filter((w) => !walls.has(w));
        if (rooms.size) current.rooms = current.rooms.filter((r) => !rooms.has(r));
        if (markers.size) current.markers = current.markers.filter((m) => !markers.has(m));
        for (const target of targets) {
            if (target.kind === "opening") target.wall.openings = target.wall.openings.filter((o) => o !== target.opening);
        }
        clearSelection();
        renderSidebar();
        markDirty();
    }

    function setTool(tool: Tool): void {
        if (state.drawing.length) commitChain();
        state.tool = tool;
        for (const button of document.querySelectorAll<HTMLButtonElement>("#floorplan-tools button")) {
            button.classList.toggle("is-active", button.dataset.tool === tool);
            button.setAttribute("aria-pressed", String(button.dataset.tool === tool));
        }
        mapEl.classList.toggle("is-drawing", tool !== "select");
        const hint = document.getElementById("floorplan-hint");
        if (hint) {
            // Select needs no hint - the interaction is the ordinary "click a
            // thing to work on it" every other tool on the site already uses.
            hint.textContent = tool === "wall" ? "Click to place corners · click the first corner to close · Esc finishes · Alt disables snapping" : tool === "marker" ? "Click to drop a marker" : "";
        }
        renderSidebar();
    }

    // ------------------------------------------------------------- sidebar

    function renameFloor(item: Floor): void {
        const value = window.prompt("Floor name", item.name || `Level ${item.level}`);
        if (value === null) return;
        item.name = value.trim();
        markDirty();
        renderSidebar();
    }

    function deleteFloor(index: number): void {
        const item = state.doc.floors[index] as Floor;
        if (state.doc.floors.length <= 1) return;
        if (!window.confirm(`Delete "${item.name || `Level ${item.level}`}"? This removes everything drawn on it.`)) return;
        state.doc.floors.splice(index, 1);
        state.floorIndex = Math.min(state.floorIndex, state.doc.floors.length - 1);
        clearSelection();
        renderSidebar();
        markDirty();
    }

    function renderFloorTabs(): void {
        const host = document.getElementById("floorplan-floors");
        if (!host) return;
        host.replaceChildren();
        state.doc.floors.forEach((item, index) => {
            const tab = document.createElement("span");
            tab.className = "floorplan-floor-tab";
            const button = document.createElement("button");
            button.type = "button";
            button.className = `btn btn--sm${index === state.floorIndex ? " btn--primary" : " btn--ghost"}`;
            button.textContent = item.name || `Level ${item.level}`;
            button.title = "Double-click to rename";
            button.addEventListener("click", () => {
                state.floorIndex = index;
                clearSelection();
                renderSidebar();
                render();
                fitToContent();
            });
            button.addEventListener("dblclick", () => renameFloor(item));
            tab.appendChild(button);
            if (state.doc.floors.length > 1) {
                const remove = document.createElement("button");
                remove.type = "button";
                remove.className = "btn btn--icon-sm floorplan-floor-tab__delete";
                remove.innerHTML = '<i class="material-symbols-outlined">close</i>';
                remove.setAttribute("aria-label", `Delete ${item.name || "floor"}`);
                remove.title = "Delete floor";
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
            const levels = state.doc.floors.map((f) => f.level);
            const level = (levels.length ? Math.max(...levels) : -1) + 1;
            const added: Floor = { level, name: `Level ${level}`, walls: [], rooms: [], markers: [] };
            state.doc.floors.push(added);
            state.floorIndex = state.doc.floors.length - 1;
            if (seedFromOutline(added)) toast.info("Started this floor from the building outline.");
            markDirty();
            renderSidebar();
        });
        host.appendChild(add);
    }

    function field(labelText: string, input: HTMLElement): HTMLLabelElement {
        const label = document.createElement("label");
        label.className = "floorplan-field";
        const span = document.createElement("span");
        span.textContent = labelText;
        label.append(span, input);
        return label;
    }

    function select(options: string[], value: string, onChange: (v: string) => void): HTMLSelectElement {
        const node = document.createElement("select");
        node.className = "form-input";
        for (const option of options) {
            const item = document.createElement("option");
            item.value = option;
            item.textContent = option;
            if (option === value) item.selected = true;
            node.appendChild(item);
        }
        node.addEventListener("change", () => onChange(node.value));
        return node;
    }

    function renderSidebar(): void {
        const host = document.getElementById("floorplan-form");
        if (!host) return;
        host.replaceChildren();
        const selection = state.selection;
        if (!selection) {
            const hint = document.createElement("p");
            hint.className = "floorplan-hint";
            hint.textContent = "Nothing selected.";
            host.appendChild(hint);
            return;
        }

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
                for (const kind of ["exterior", "interior", "virtual", "collapsed"]) {
                    const item = document.createElement("option");
                    item.value = kind;
                    item.textContent = kind;
                    typeSelect.appendChild(item);
                }
                typeSelect.addEventListener("change", () => {
                    if (!typeSelect.value) return;
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
                    select(["exterior", "interior", "virtual", "collapsed"], wall.kind, (v) => {
                        wall.kind = v as Wall["kind"];
                        markDirty();
                    }),
                ),
            );
            host.appendChild(
                field(
                    "Thickness",
                    select(["thin", "normal", "thick"], wall.thickness, (v) => {
                        wall.thickness = v as Wall["thickness"];
                        markDirty();
                    }),
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
                    select(["door", "doorway", "window", "hatch"], opening.kind, (v) => {
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
                room.name = name.value;
                state.dirty = true;
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
                marker.name = name.value;
                state.dirty = true;
            });
            host.appendChild(field("Label", name));
            host.appendChild(
                field(
                    "Type",
                    select(Object.keys(MARKER_ICON), marker.kind, (v) => {
                        marker.kind = v as MarkerKind;
                        markDirty();
                    }),
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
                    select(["door", "doorway", "window", "hatch"], opening.kind, (v) => {
                        opening.kind = v as Opening["kind"];
                        markDirty();
                    }),
                ),
            );
        }

        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "btn btn--sm btn--danger";
        remove.textContent = "Delete";
        remove.addEventListener("click", deleteSelection);
        host.appendChild(remove);
    }

    /**
     * Link a stair or lift to its counterpart on an adjacent floor.
     *
     * Two markers sharing a ``connector_id`` are the same physical shaft. The
     * id is authored here rather than derived from position because a
     * switchback stair genuinely lands somewhere else on the floor above, so
     * proximity would be wrong exactly when it mattered.
     */
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
                .map((item) => item.name || `Level ${item.level}`);
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
                const where = candidate.floor.name || `Level ${candidate.floor.level}`;
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
        empty.hidden = current.walls.length > 0;
    }

    // ------------------------------------------------------------ persistence

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
            }
        } catch {
            toast.warning("Could not load this floorplan.");
        }
        const anchor = state.doc.plan_origin || { lat, lng };
        state.doc.plan_origin = anchor;
        projection = new PlanProjection(anchor);
        const nameInput = document.getElementById("floorplan-name") as HTMLInputElement | null;
        if (nameInput) nameInput.value = state.doc.name || "";
        const validFrom = document.getElementById("floorplan-valid-from") as HTMLInputElement | null;
        if (validFrom) validFrom.value = state.doc.valid_from || "";
        // A brand-new plan starts from the real footprint when one is known, so
        // the first thing on screen is the building rather than a blank map.
        const fresh = state.doc.floors.length === 1 && !(state.doc.floors[0] as Floor).walls.length;
        if (fresh && seedFromOutline(state.doc.floors[0] as Floor)) state.dirty = true;
        else state.dirty = false;
        setTool("select");
        render();
        fitToContent();
        updateMoreMenu();
    }

    async function save(asNewVersion = false): Promise<void> {
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
        // instead of overwriting the one that was loaded.
        if (asNewVersion) delete payload.uuid;
        delete payload.origin;
        delete payload.versions;

        const button = document.getElementById("floorplan-save") as HTMLButtonElement | null;
        if (button) button.disabled = true;
        try {
            const response = await fetch(saveUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                body: JSON.stringify(payload),
            });
            if (!response.ok) {
                const text = await response.text();
                toast.warning(text || "Could not save this floorplan.");
                return;
            }
            // The save view answers {ok, floorplan: <document>} - the uuid is
            // nested, and picking it up is what makes the next save update
            // this version instead of forking another one.
            const body = (await response.json()) as { ok?: boolean; floorplan?: FloorplanDocument };
            if (body.floorplan?.uuid) state.doc.uuid = body.floorplan.uuid;
            state.dirty = false;
            updateMoreMenu();
            toast.success(asNewVersion ? "Saved as a new version." : "Floorplan saved.");
        } catch {
            toast.warning("Could not save this floorplan.");
        } finally {
            if (button) button.disabled = false;
        }
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

    document.getElementById("floorplan-save")?.addEventListener("click", () => void save(false));
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
        })();
    });

    for (const button of document.querySelectorAll<HTMLButtonElement>("[data-tool]")) {
        button.addEventListener("click", () => setTool((button.dataset.tool as Tool) || "select"));
    }
    for (const button of document.querySelectorAll<HTMLButtonElement>("[data-marker-kind]")) {
        button.addEventListener("click", () => {
            state.markerKind = button.dataset.markerKind as MarkerKind;
            setTool("marker");
        });
    }
    document.getElementById("floorplan-start-outline")?.addEventListener("click", () => {
        if (seedFromOutline(floor())) markDirty();
        else toast.info("No building outline is known for this place yet.");
    });
    document.getElementById("floorplan-start-rectangle")?.addEventListener("click", () => {
        // Four walls around the current view's middle third: the fastest way to
        // show that closing a loop produces a room.
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
