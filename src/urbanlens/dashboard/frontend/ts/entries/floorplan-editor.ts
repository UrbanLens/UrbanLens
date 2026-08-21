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
import { tileLayer } from "../shared/map-layers";

declare const L: typeof import("leaflet");

type Tool = "select" | "wall" | "marker";

const WALL_STYLE: Record<string, { color: string; weight: number; dashArray?: string }> = {
    exterior: { color: "#263238", weight: 5 },
    interior: { color: "#546e7a", weight: 3 },
    virtual: { color: "#90a4ae", weight: 2, dashArray: "6 6" },
    collapsed: { color: "#a1887f", weight: 3, dashArray: "2 6" },
};

const MARKER_ICON: Record<MarkerKind, string> = {
    photo: "photo_camera",
    hazard: "warning",
    entrance: "door_open",
    stair: "stairs",
    elevator: "elevator",
    note: "sticky_note_2",
    fixture: "settings",
};

const ROOM_FILL = { color: "#00897b", weight: 1, fillColor: "#26a69a", fillOpacity: 0.16 };
const UNBOUND_FILL = { color: "#b0bec5", weight: 1, dashArray: "4 4", fillOpacity: 0.05 };

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

    const map = L.map("floorplan-map", { zoomControl: true, doubleClickZoom: false }).setView([lat, lng], 20);
    tileLayer("satellite", { maxZoom: 22, maxNativeZoom: 19 }).addTo(map);

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

    const state = {
        doc: emptyDocument({ lat, lng }),
        floorIndex: 0,
        tool: "select" as Tool,
        /** Chain of points for the wall currently being drawn. */
        drawing: [] as Pt[],
        cursor: null as Pt | null,
        snapKind: "" as string,
        selection: null as { kind: "wall"; wall: Wall } | { kind: "room"; room: RoomSeed } | { kind: "marker"; marker: Marker } | null,
        markerKind: "photo" as MarkerKind,
        dirty: false,
        suspendSnap: false,
        faces: [] as Face[],
        versions: [] as VersionSummary[],
    };

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

        // Rooms first so walls draw on top of their fills.
        for (const face of derived.faces) {
            const seed = current.rooms.find((room) => faceForSeed({ x: room.x, y: room.y }, [face]) === face);
            const polygon = L.polygon(face.ring.map(toLatLng), seed ? ROOM_FILL : UNBOUND_FILL).addTo(roomLayer);
            const label = seed ? seed.name || "Unnamed" : "Unnamed room";
            polygon.bindTooltip(`${label} · ${face.area.toFixed(1)} m²`, { direction: "center", className: "floorplan-room-label" });
            polygon.on("click", (event) => {
                L.DomEvent.stop(event);
                if (state.tool !== "select") return;
                const bound = seed || addSeedAt(centroid(face.ring));
                state.selection = { kind: "room", room: bound };
                renderSidebar();
                render();
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
            const selected = state.selection?.kind === "wall" && state.selection.wall === wall;
            const line = L.polyline([toLatLng({ x: wall.ax, y: wall.ay }), toLatLng({ x: wall.bx, y: wall.by })], {
                ...style,
                ...(selected ? { color: "#00838f", weight: (style?.weight || 3) + 2 } : {}),
            }).addTo(wallLayer);
            line.on("click", (event) => {
                L.DomEvent.stop(event);
                if (state.tool !== "select") return;
                state.selection = { kind: "wall", wall };
                renderSidebar();
                render();
            });
            renderOpenings(wall);
            if (selected) renderWallHandles(wall);
        }

        for (const marker of current.markers) {
            const icon = L.divIcon({
                className: "floorplan-marker",
                html: `<span class="material-symbols-outlined">${MARKER_ICON[marker.kind] || "place"}</span>`,
                iconSize: [24, 24],
            });
            const node = L.marker(toLatLng({ x: marker.x, y: marker.y }), { icon, draggable: state.tool === "select" }).addTo(markerLayer);
            node.bindTooltip(marker.name || marker.kind, { direction: "top" });
            node.on("click", (event) => {
                L.DomEvent.stop(event);
                state.selection = { kind: "marker", marker };
                renderSidebar();
                render();
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

        renderFloorTabs();
        updateEmptyState(current);
    }

    function renderOpenings(wall: Wall): void {
        const a = { x: wall.ax, y: wall.ay };
        const b = { x: wall.bx, y: wall.by };
        for (const opening of wall.openings) {
            const at = (t: number): Pt => ({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t });
            const colour = opening.kind === "window" ? "#1e88e5" : "#fb8c00";
            L.polyline([toLatLng(at(opening.t_start)), toLatLng(at(opening.t_end))], { color: colour, weight: 6 })
                .bindTooltip(opening.kind, { direction: "top" })
                .addTo(wallLayer);
        }
    }

    function renderWallHandles(wall: Wall): void {
        for (const end of ["a", "b"] as const) {
            const point = end === "a" ? { x: wall.ax, y: wall.ay } : { x: wall.bx, y: wall.by };
            const handle = L.circleMarker(toLatLng(point), { radius: 6, color: "#00838f", fillColor: "#fff", fillOpacity: 1 }).addTo(handleLayer);
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

    function centroid(ring: readonly Pt[]): Pt {
        let x = 0;
        let y = 0;
        for (const p of ring) {
            x += p.x;
            y += p.y;
        }
        return { x: x / ring.length, y: y / ring.length };
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
            floor().markers.push({ uuid: nextLocalId(), kind: state.markerKind, x: raw.x, y: raw.y, name: "" });
            markDirty();
            return;
        }
        state.selection = null;
        renderSidebar();
        render();
    });

    map.on("dblclick", () => {
        if (state.tool === "wall") commitChain();
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Alt") state.suspendSnap = true;
        if (event.key === "Escape") {
            if (state.drawing.length) commitChain();
            else {
                state.selection = null;
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
        const selection = state.selection;
        if (!selection) return;
        if (selection.kind === "wall") current.walls = current.walls.filter((w) => w !== selection.wall);
        if (selection.kind === "room") current.rooms = current.rooms.filter((r) => r !== selection.room);
        if (selection.kind === "marker") current.markers = current.markers.filter((m) => m !== selection.marker);
        state.selection = null;
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
            hint.textContent =
                tool === "wall"
                    ? "Click to place corners · click the first corner to close · Esc finishes · Alt disables snapping"
                    : tool === "marker"
                      ? "Click to drop a marker"
                      : "Click a wall or room to edit it · Delete removes it";
        }
        renderSidebar();
    }

    // ------------------------------------------------------------- sidebar

    function renderFloorTabs(): void {
        const host = document.getElementById("floorplan-floors");
        if (!host) return;
        host.replaceChildren();
        state.doc.floors.forEach((item, index) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = `btn btn--sm${index === state.floorIndex ? " btn--primary" : " btn--ghost"}`;
            button.textContent = item.name || `Level ${item.level}`;
            button.addEventListener("click", () => {
                state.floorIndex = index;
                state.selection = null;
                renderSidebar();
                render();
            });
            host.appendChild(button);
        });
        const add = document.createElement("button");
        add.type = "button";
        add.className = "btn btn--sm btn--ghost";
        add.textContent = "+ Floor";
        add.addEventListener("click", () => {
            const levels = state.doc.floors.map((f) => f.level);
            const level = (levels.length ? Math.max(...levels) : -1) + 1;
            state.doc.floors.push({ level, name: `Level ${level}`, walls: [], rooms: [], markers: [] });
            state.floorIndex = state.doc.floors.length - 1;
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
            host.appendChild(
                field(
                    "Type",
                    select(Object.keys(MARKER_ICON), marker.kind, (v) => {
                        marker.kind = v as MarkerKind;
                        markDirty();
                    }),
                ),
            );
            const name = document.createElement("input");
            name.className = "form-input";
            name.value = marker.name || "";
            name.addEventListener("input", () => {
                marker.name = name.value;
                state.dirty = true;
            });
            host.appendChild(field("Label", name));
        }

        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "btn btn--sm btn--danger";
        remove.textContent = "Delete";
        remove.addEventListener("click", deleteSelection);
        host.appendChild(remove);
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
        state.dirty = false;
        setTool("select");
        render();
    }

    async function save(asNewVersion = false): Promise<void> {
        const nameInput = document.getElementById("floorplan-name") as HTMLInputElement | null;
        const validFrom = document.getElementById("floorplan-valid-from") as HTMLInputElement | null;
        state.doc.name = nameInput?.value || "";
        state.doc.valid_from = validFrom?.value || null;
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
            toast.success(asNewVersion ? "Saved as a new version." : "Floorplan saved.");
        } catch {
            toast.warning("Could not save this floorplan.");
        } finally {
            if (button) button.disabled = false;
        }
    }

    document.getElementById("floorplan-save")?.addEventListener("click", () => void save(false));
    document.getElementById("floorplan-save-version")?.addEventListener("click", () => void save(true));
    document.getElementById("floorplan-publish")?.addEventListener("click", () => {
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
