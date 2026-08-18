/**
 * Visual floorplan editor: draw a building's interior over real imagery.
 *
 * Loads the pin's floorplan document (REData's document shape - see
 * services/floorplans/serialization.py), renders each floor's rooms, walls
 * and openings as editable Leaflet layers over the normal base layers
 * (satellite by default - the point is tracing over what's really there) and
 * the pin's georeferenced blueprint overlays, and saves the whole document
 * back in one POST.
 *
 * Config comes from data-* attributes on `#floorplan-map` plus JSON script
 * tags (overlays, labels), matching the map-annotations pattern.
 */
import { getCsrfToken } from "../shared/csrf";
import { toast } from "../shared/dialogs";
import { createMapImageOverlays, type MapOverlayEntry } from "../shared/map-image-overlays";
import { tileLayer } from "../shared/map-layers";

declare const L: typeof import("leaflet");

type Geometry = { type: string; coordinates: unknown } | null;

interface Lock {
    uuid?: string;
    name: string;
    key_attributes: Record<string, unknown>;
    [key: string]: unknown;
}

interface Item {
    uuid?: string;
    description?: string;
    condition?: string;
    built_date?: string | null;
    attributes?: Record<string, unknown>;
    source?: string | null;
    references?: string[];
    labels?: string[];
}

interface ElementItem extends Item {
    kind: string;
    name?: string;
    geometry?: Geometry;
    material?: string;
    room?: string | null;
    mounted_on?: string | null;
    base_elevation_meters?: number | null;
    height_meters?: number | null;
    locks?: Lock[];
}

interface RoomItem extends Item {
    name?: string;
    geometry?: Geometry;
    height_meters?: number | null;
}

interface FloorItem extends Item {
    level: number;
    name?: string;
    geometry?: Geometry;
    elevation_meters?: number | null;
    height_meters?: number | null;
    rooms?: RoomItem[];
    elements?: ElementItem[];
}

interface FloorplanDocument extends Item {
    name?: string;
    building_ref?: string;
    building_name?: string;
    valid_from?: string | null;
    floor_count?: number | null;
    source_pool?: Record<string, unknown>[];
    reference_pool?: Record<string, unknown>[];
    floors?: FloorItem[];
    elements?: ElementItem[];
    origin?: string;
}

const KIND_STYLE: Record<string, { color: string; weight: number }> = {
    wall: { color: "#37474f", weight: 4 },
    door: { color: "#ef6c00", weight: 3 },
    window: { color: "#1976d2", weight: 3 },
    stair: { color: "#6a1b9a", weight: 3 },
    column: { color: "#455a64", weight: 3 },
    fixture: { color: "#00695c", weight: 3 },
    other: { color: "#616161", weight: 3 },
};
const ROOM_STYLE = { color: "#00897b", weight: 2, fillColor: "#00897b", fillOpacity: 0.12 };
const OUTLINE_STYLE = { color: "#8d6e63", weight: 2, dashArray: "6 4", fillOpacity: 0.03 };

function boot(): void {
    const mapElNode = document.getElementById("floorplan-map");
    if (!mapElNode) return;
    const mapEl = mapElNode;
    const jsonUrl = mapEl.dataset.jsonUrl || "";
    const saveUrl = mapEl.dataset.saveUrl || "";
    const lat = parseFloat(mapEl.dataset.lat || "0");
    const lng = parseFloat(mapEl.dataset.lng || "0");

    const map = L.map("floorplan-map", { zoomControl: true }).setView([lat, lng], 19);
    tileLayer("satellite", { maxZoom: 22, maxNativeZoom: 19 }).addTo(map);

    const extractUrlTemplate = mapEl.dataset.extractUrl || "";
    const overlays = readJson<MapOverlayEntry[]>("floorplan-overlays") || [];
    if (overlays.length) {
        // Read-only here: aligning corners happens in the pin map's own
        // overlay manager; the editor just shows the sheets for tracing.
        const overlayControl = createMapImageOverlays(L, map, {
            cornersUrl: () => "",
            csrfToken: getCsrfToken(),
        });
        overlayControl.sync(overlays.map((entry) => ({ ...entry, locked: true })));
    }
    const profileLabels = readJson<{ uuid: string; name: string }[]>("floorplan-labels") || [];

    const state = {
        doc: null as FloorplanDocument | null,
        floorIndex: 0,
        tool: "select" as string,
        selected: null as { type: "room" | "element" | "floor"; item: RoomItem | ElementItem | FloorItem } | null,
        drawing: null as { latlngs: [number, number][]; preview: L.Layer | null } | null,
        dirty: false,
    };
    const itemLayers = L.layerGroup().addTo(map);
    const layerByItem = new Map<object, L.Layer>();

    const emptyDoc = (): FloorplanDocument => ({
        name: "",
        valid_from: null,
        source_pool: [],
        reference_pool: [],
        floors: [{ level: 0, name: "Ground floor", rooms: [], elements: [] }],
        elements: [],
    });

    function currentFloor(): FloorItem {
        const doc = state.doc as FloorplanDocument;
        const floors = (doc.floors = doc.floors || []);
        if (!floors.length) floors.push({ level: 0, name: "Ground floor", rooms: [], elements: [] });
        state.floorIndex = Math.min(state.floorIndex, floors.length - 1);
        return floors[state.floorIndex] as FloorItem;
    }

    // ---------- rendering ----------

    function render(): void {
        itemLayers.clearLayers();
        layerByItem.clear();
        renderFloorTabs();
        const floor = currentFloor();
        if (floor.geometry) addLayer(floor, geometryToLayer(floor.geometry, OUTLINE_STYLE), "floor");
        for (const room of floor.rooms || []) {
            if (!room.geometry) continue;
            const layer = geometryToLayer(room.geometry, ROOM_STYLE);
            if (room.name && "getBounds" in (layer as L.Polygon)) {
                (layer as L.Polygon).bindTooltip(room.name, { permanent: true, direction: "center", className: "floorplan-room-label" });
            }
            addLayer(room, layer, "room");
        }
        for (const element of floor.elements || []) {
            if (!element.geometry) continue;
            const style = KIND_STYLE[element.kind] || (KIND_STYLE.other as { color: string; weight: number });
            addLayer(element, geometryToLayer(element.geometry, style), "element");
        }
        renderForm();
        const saveButton = document.getElementById("floorplan-save");
        if (saveButton) saveButton.classList.toggle("is-dirty", state.dirty);
    }

    function geometryToLayer(geometry: NonNullable<Geometry>, style: L.PathOptions): L.Layer {
        if (geometry.type === "Point") {
            const [x, y] = geometry.coordinates as [number, number];
            return L.circleMarker([y, x], { radius: 7, ...style, fillOpacity: 0.9 });
        }
        return L.geoJSON({ type: "Feature", geometry, properties: {} } as GeoJSON.Feature, { style: () => style });
    }

    function addLayer(item: object, layer: L.Layer, type: "room" | "element" | "floor"): void {
        layer.on("click", (event: L.LeafletEvent) => {
            if (state.tool !== "select") return;
            L.DomEvent.stopPropagation(event as L.LeafletMouseEvent);
            state.selected = { type, item: item as RoomItem & ElementItem & FloorItem };
            render();
        });
        if (state.selected && state.selected.item === item && "setStyle" in (layer as L.Path)) {
            (layer as L.Path).setStyle({ color: "#d81b60", weight: 4 });
        }
        itemLayers.addLayer(layer);
        layerByItem.set(item, layer);
    }

    function renderFloorTabs(): void {
        const host = document.getElementById("floorplan-floors");
        if (!host || !state.doc) return;
        host.innerHTML = "";
        (state.doc.floors || []).forEach((floor, index) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "floorplan-floor-tab" + (index === state.floorIndex ? " is-active" : "");
            button.textContent = floor.name || `Level ${floor.level}`;
            button.addEventListener("click", () => {
                state.floorIndex = index;
                state.selected = null;
                render();
            });
            host.appendChild(button);
        });
        const add = document.createElement("button");
        add.type = "button";
        add.className = "floorplan-floor-tab floorplan-floor-tab--add";
        add.textContent = "+ Floor";
        add.addEventListener("click", () => {
            const floors = state.doc?.floors || [];
            const nextLevel = floors.length ? Math.max(...floors.map((f) => f.level)) + 1 : 0;
            floors.push({ level: nextLevel, name: `Level ${nextLevel}`, rooms: [], elements: [] });
            state.floorIndex = floors.length - 1;
            markDirty();
            render();
        });
        host.appendChild(add);
    }

    // ---------- property form ----------

    function renderForm(): void {
        const host = document.getElementById("floorplan-form");
        if (!host) return;
        host.innerHTML = "";
        if (!state.selected) {
            host.innerHTML = '<p class="floorplan-form__hint">Select an item on the map, or pick a tool and draw. Blueprint overlays added to this pin show underneath for tracing.</p>';
            return;
        }
        const { type, item } = state.selected;
        const element = item as ElementItem;
        const fields: [string, string, string][] = [["name", "Name", (item as RoomItem).name || ""]];
        if (type === "element") fields.push(["material", "Material", element.material || ""]);
        fields.push(["condition", "Condition", (item as Item).condition || ""], ["built_date", "Built date (YYYY-MM-DD)", (item as Item).built_date || ""]);

        for (const [key, label, value] of fields) {
            host.appendChild(fieldRow(label, value, (next) => {
                (item as Record<string, unknown>)[key] = next || (key === "built_date" ? null : "");
                markDirty();
                if (key === "name") render();
            }));
        }
        const description = document.createElement("textarea");
        description.className = "floorplan-form__textarea";
        description.placeholder = "Description";
        description.value = (item as Item).description || "";
        description.addEventListener("input", () => {
            (item as Item).description = description.value;
            markDirty();
        });
        host.appendChild(description);

        if (profileLabels.length) host.appendChild(labelPicker(item as Item));
        if (type === "element" && element.kind === "door") host.appendChild(locksEditor(element));

        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "floorplan-form__delete";
        remove.textContent = "Delete";
        remove.addEventListener("click", () => {
            const floor = currentFloor();
            if (type === "room") floor.rooms = (floor.rooms || []).filter((r) => r !== item);
            else if (type === "element") floor.elements = (floor.elements || []).filter((e) => e !== item);
            else floor.geometry = null;
            state.selected = null;
            markDirty();
            render();
        });
        host.appendChild(remove);
    }

    function fieldRow(label: string, value: string, onInput: (value: string) => void): HTMLElement {
        const wrap = document.createElement("label");
        wrap.className = "floorplan-form__field";
        const caption = document.createElement("span");
        caption.textContent = label;
        const input = document.createElement("input");
        input.type = "text";
        input.value = value;
        input.addEventListener("input", () => onInput(input.value));
        wrap.append(caption, input);
        return wrap;
    }

    function labelPicker(item: Item): HTMLElement {
        const wrap = document.createElement("div");
        wrap.className = "floorplan-form__labels";
        const caption = document.createElement("span");
        caption.textContent = "Labels";
        wrap.appendChild(caption);
        const chosen = new Set(item.labels || []);
        for (const label of profileLabels) {
            const row = document.createElement("label");
            const box = document.createElement("input");
            box.type = "checkbox";
            box.checked = chosen.has(label.uuid);
            box.addEventListener("change", () => {
                if (box.checked) chosen.add(label.uuid);
                else chosen.delete(label.uuid);
                item.labels = Array.from(chosen);
                markDirty();
            });
            row.append(box, document.createTextNode(label.name));
            wrap.appendChild(row);
        }
        return wrap;
    }

    function locksEditor(element: ElementItem): HTMLElement {
        const wrap = document.createElement("div");
        wrap.className = "floorplan-form__locks";
        const caption = document.createElement("span");
        caption.textContent = "Locks";
        wrap.appendChild(caption);
        const locks = (element.locks = element.locks || []);
        locks.forEach((lock, index) => {
            const row = document.createElement("div");
            row.className = "floorplan-form__lock";
            const name = document.createElement("input");
            name.type = "text";
            name.placeholder = "Lock type (padlock, deadbolt...)";
            name.value = lock.name || "";
            name.addEventListener("input", () => {
                lock.name = name.value;
                markDirty();
            });
            const keys = document.createElement("input");
            keys.type = "text";
            keys.placeholder = "Key attributes (brand=Abus, keyway=AB1)";
            keys.value = Object.entries(lock.key_attributes || {}).map(([k, v]) => `${k}=${v}`).join(", ");
            keys.addEventListener("input", () => {
                const attributes: Record<string, unknown> = {};
                for (const pair of keys.value.split(",")) {
                    const [k, ...rest] = pair.split("=");
                    if (k && k.trim() && rest.length) attributes[k.trim()] = rest.join("=").trim();
                }
                lock.key_attributes = attributes;
                markDirty();
            });
            const remove = document.createElement("button");
            remove.type = "button";
            remove.textContent = "×";
            remove.addEventListener("click", () => {
                locks.splice(index, 1);
                markDirty();
                renderForm();
            });
            row.append(name, keys, remove);
            wrap.appendChild(row);
        });
        const add = document.createElement("button");
        add.type = "button";
        add.className = "floorplan-form__add-lock";
        add.textContent = "+ Add lock";
        add.addEventListener("click", () => {
            locks.push({ name: "", key_attributes: {} });
            markDirty();
            renderForm();
        });
        wrap.appendChild(add);
        return wrap;
    }

    // ---------- drawing ----------

    const POLYGON_TOOLS = new Set(["room", "outline"]);
    const LINE_TOOLS = new Set(["wall"]);
    const POINT_TOOLS = new Set(["door", "window", "stair", "fixture", "column", "other"]);

    function setTool(tool: string): void {
        state.tool = tool;
        state.drawing = null;
        state.selected = null;
        document.querySelectorAll<HTMLButtonElement>("#floorplan-tools button").forEach((button) => {
            button.classList.toggle("is-active", button.dataset.tool === tool);
        });
        mapEl.classList.toggle("is-drawing", tool !== "select");
        render();
    }

    map.on("click", (event: L.LeafletMouseEvent) => {
        if (state.tool === "select") return;
        const floor = currentFloor();
        const point: [number, number] = [event.latlng.lng, event.latlng.lat];
        if (POINT_TOOLS.has(state.tool)) {
            (floor.elements = floor.elements || []).push({ kind: state.tool, geometry: { type: "Point", coordinates: point }, locks: [] });
            markDirty();
            render();
            return;
        }
        const drawing = (state.drawing = state.drawing || { latlngs: [], preview: null });
        drawing.latlngs.push(point);
        if (drawing.preview) itemLayers.removeLayer(drawing.preview as L.Layer);
        const latlngs = drawing.latlngs.map(([x, y]) => [y, x] as [number, number]);
        drawing.preview = POLYGON_TOOLS.has(state.tool)
            ? L.polygon(latlngs, { color: "#d81b60", dashArray: "4 4", fillOpacity: 0.05 })
            : L.polyline(latlngs, { color: "#d81b60", dashArray: "4 4" });
        itemLayers.addLayer(drawing.preview);
    });

    map.on("dblclick", (event: L.LeafletMouseEvent) => {
        if (state.tool === "select" || !state.drawing) return;
        L.DomEvent.stop(event);
        finishShape();
    });

    function finishShape(): void {
        const drawing = state.drawing;
        if (!drawing) return;
        const floor = currentFloor();
        const points = drawing.latlngs;
        state.drawing = null;
        if (POLYGON_TOOLS.has(state.tool) && points.length >= 3) {
            const ring = [...points, points[0]];
            const geometry = { type: "Polygon", coordinates: [ring] };
            if (state.tool === "room") (floor.rooms = floor.rooms || []).push({ name: "", geometry });
            else floor.geometry = geometry;
            markDirty();
        } else if (LINE_TOOLS.has(state.tool) && points.length >= 2) {
            (floor.elements = floor.elements || []).push({ kind: "wall", geometry: { type: "LineString", coordinates: points } });
            markDirty();
        }
        render();
    }

    document.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && state.drawing) finishShape();
        if (event.key === "Escape") {
            state.drawing = null;
            setTool("select");
        }
    });

    // ---------- persistence ----------

    function markDirty(): void {
        state.dirty = true;
        const saveButton = document.getElementById("floorplan-save");
        if (saveButton) saveButton.classList.add("is-dirty");
    }

    async function save(): Promise<void> {
        if (!state.doc) return;
        const dateInput = document.getElementById("floorplan-valid-from") as HTMLInputElement | null;
        if (dateInput) state.doc.valid_from = dateInput.value || null;
        const nameInput = document.getElementById("floorplan-name") as HTMLInputElement | null;
        if (nameInput) state.doc.name = nameInput.value;
        delete state.doc.origin;
        const response = await fetch(saveUrl, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
            body: JSON.stringify(state.doc),
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok || !body.ok) {
            toast.error(body.error || "Could not save the floorplan.");
            return;
        }
        state.doc = body.floorplan;
        state.dirty = false;
        toast.success("Floorplan saved.");
        render();
    }

    async function load(): Promise<void> {
        const params = new URLSearchParams(window.location.search);
        const url = params.get("date") ? `${jsonUrl}?date=${encodeURIComponent(params.get("date") as string)}` : jsonUrl;
        const response = await fetch(url, { headers: { Accept: "application/json" } });
        state.doc = response.status === 204 ? emptyDoc() : ((await response.json()) as FloorplanDocument);
        if (state.doc.origin === "redata") {
            const banner = document.getElementById("floorplan-origin-banner");
            if (banner) banner.hidden = false;
        }
        const nameInput = document.getElementById("floorplan-name") as HTMLInputElement | null;
        if (nameInput) nameInput.value = state.doc.name || "";
        const dateInput = document.getElementById("floorplan-valid-from") as HTMLInputElement | null;
        if (dateInput) dateInput.value = state.doc.valid_from || "";
        const first = (state.doc.floors || []).find((floor) => floor.geometry || (floor.rooms || []).length || (floor.elements || []).length);
        fitTo(first);
        render();
    }

    function fitTo(floor: FloorItem | undefined): void {
        if (!floor) return;
        const layer = floor.geometry ? geometryToLayer(floor.geometry, { weight: 1 }) : null;
        const bounds = layer && "getBounds" in (layer as L.Polygon) ? (layer as L.Polygon).getBounds() : null;
        if (bounds && bounds.isValid()) map.fitBounds(bounds.pad(0.3));
    }

    async function traceOverlay(uuid: string, button: HTMLButtonElement): Promise<void> {
        button.disabled = true;
        button.textContent = "Tracing...";
        try {
            const response = await fetch(extractUrlTemplate.replace("00000000-0000-0000-0000-000000000000", uuid), {
                method: "POST",
                headers: { "X-CSRFToken": getCsrfToken() },
            });
            const body = await response.json().catch(() => ({}));
            if (!response.ok || !body.ok) {
                toast.warning(body.error || "Couldn't trace this sheet.");
                return;
            }
            const floor = currentFloor();
            const rooms = (body.rooms as RoomItem[]) || [];
            const elements = (body.elements as ElementItem[]) || [];
            (floor.rooms = floor.rooms || []).push(...rooms);
            (floor.elements = floor.elements || []).push(...elements);
            markDirty();
            render();
            toast.success(`Traced ${rooms.length} room${rooms.length === 1 ? "" : "s"} and ${elements.length} element${elements.length === 1 ? "" : "s"} - drag, rename, and delete to correct them, then save.`);
        } finally {
            button.disabled = false;
            button.textContent = "Auto-trace";
        }
    }

    function renderTraceButtons(): void {
        const host = document.getElementById("floorplan-trace-list");
        if (!host || !overlays.length || !extractUrlTemplate) return;
        host.hidden = false;
        for (const entry of overlays) {
            const row = document.createElement("div");
            row.className = "floorplan-trace-row";
            const name = document.createElement("span");
            name.textContent = entry.name || "Blueprint";
            const button = document.createElement("button");
            button.type = "button";
            button.textContent = "Auto-trace";
            button.title = "Recognize rooms, walls, doors and windows on this aligned sheet and add them to the current floor as editable suggestions";
            button.addEventListener("click", () => void traceOverlay(entry.uuid, button));
            row.append(name, button);
            host.appendChild(row);
        }
    }
    renderTraceButtons();

    document.getElementById("floorplan-save")?.addEventListener("click", () => void save());
    document.querySelectorAll<HTMLButtonElement>("#floorplan-tools button").forEach((button) => {
        button.addEventListener("click", () => setTool(button.dataset.tool || "select"));
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
