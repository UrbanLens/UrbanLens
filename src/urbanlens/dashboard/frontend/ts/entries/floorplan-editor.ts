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
import {
    closeRing,
    dropTrailingDuplicates,
    insertVertex,
    isClosedRing,
    midpoint,
    minimumVertices,
    moveVertex,
    removeVertex,
    ringOf,
    vertexCount,
} from "../shared/floorplan-geometry";
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
    thickness_meters?: number | null;
    rotation_degrees?: number | null;
    connects_rooms?: string[];
    spans_floors?: string[];
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

interface VersionSummary {
    uuid: string;
    name: string;
    valid_from: string | null;
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
    versions?: VersionSummary[];
}

const LOCAL_NAMESPACE = "urbanlens";

/**
 * The attribute keys REData publishes for each element kind. Free-form
 * attributes stay free-form; these are the ones with an agreed meaning, so
 * they get real inputs instead of being dead weight in a JSON bag.
 */
const KIND_ATTRIBUTES: Record<string, { key: string; label: string; options?: string[] }[]> = {
    door: [
        { key: "swing", label: "Swing", options: ["", "inward", "outward", "sliding", "folding"] },
        { key: "hinge", label: "Hinge", options: ["", "left", "right"] },
        { key: "fire_rating", label: "Fire rating" },
    ],
    window: [
        { key: "glazing", label: "Glazing", options: ["", "single", "double", "triple"] },
        { key: "operation", label: "Operation", options: ["", "fixed", "casement", "sash"] },
    ],
    stair: [
        { key: "direction", label: "Direction", options: ["", "up", "down"] },
        { key: "flights", label: "Flights" },
    ],
    wall: [{ key: "fire_rating", label: "Fire rating" }],
    ceiling: [{ key: "fire_rating", label: "Fire rating" }],
    roof: [{ key: "fire_rating", label: "Fire rating" }],
};

const KIND_STYLE: Record<string, { color: string; weight: number }> = {
    wall: { color: "#37474f", weight: 4 },
    door: { color: "#ef6c00", weight: 3 },
    window: { color: "#1976d2", weight: 3 },
    stair: { color: "#6a1b9a", weight: 3 },
    column: { color: "#455a64", weight: 3 },
    fixture: { color: "#00695c", weight: 3 },
    furniture: { color: "#8d6e63", weight: 2 },
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
    const publishUrl = mapEl.dataset.publishUrl || "";
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
    const pinPhotos = readJson<{ uuid: string; url: string; caption: string }[]>("floorplan-photos") || [];

    const state = {
        doc: null as FloorplanDocument | null,
        floorIndex: 0,
        tool: "select" as string,
        selected: null as { type: "room" | "element" | "floor"; item: RoomItem | ElementItem | FloorItem } | null,
        drawing: null as { latlngs: [number, number][]; preview: L.Layer | null } | null,
        dirty: false,
    };
    const itemLayers = L.layerGroup().addTo(map);
    const handleLayers = L.layerGroup().addTo(map);
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
        handleLayers.clearLayers();
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
        renderVertexHandles();
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
        if ((state.doc.floors || []).length > 1) {
            const remove = document.createElement("button");
            remove.type = "button";
            remove.className = "floorplan-floor-tab floorplan-floor-tab--remove";
            remove.textContent = "Delete floor";
            remove.title = "Remove this floor and everything drawn on it";
            remove.addEventListener("click", () => {
                const floors = state.doc?.floors || [];
                const floor = floors[state.floorIndex];
                const drawn = ((floor?.rooms || []).length + (floor?.elements || []).length) as number;
                if (drawn && !window.confirm(`Delete ${floor?.name || "this floor"} and the ${drawn} item${drawn === 1 ? "" : "s"} on it?`)) return;
                floors.splice(state.floorIndex, 1);
                state.floorIndex = Math.max(0, state.floorIndex - 1);
                state.selected = null;
                markDirty();
                render();
            });
            host.appendChild(remove);
        }
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

    // ---------- vertex editing ----------

    function handleIcon(kind: "vertex" | "midpoint"): L.DivIcon {
        return L.divIcon({
            className: `floorplan-handle floorplan-handle--${kind}`,
            iconSize: kind === "vertex" ? [12, 12] : [9, 9],
        });
    }

    /**
     * Draggable handles for the selected item's geometry: one per vertex,
     * plus a midpoint handle between each pair that inserts a vertex when
     * dragged. Alt-clicking a vertex removes it, down to the minimum a shape
     * needs (3 for a room, 2 for a wall).
     */
    function renderVertexHandles(): void {
        if (!state.selected || state.tool !== "select") return;
        const item = state.selected.item as { geometry?: Geometry };
        const geometry = item.geometry;
        if (!geometry) return;

        if (geometry.type === "Point") {
            const [x, y] = geometry.coordinates as [number, number];
            const marker = L.marker([y, x], { draggable: true, icon: handleIcon("vertex") });
            marker.on("drag dragend", (event: L.LeafletEvent) => {
                const position = (event.target as L.Marker).getLatLng();
                geometry.coordinates = [position.lng, position.lat];
                if (event.type === "dragend") {
                    markDirty();
                    render();
                }
            });
            handleLayers.addLayer(marker);
            return;
        }

        const ring = ringOf(geometry);
        if (!ring) return;
        const closed = isClosedRing(geometry);
        const count = vertexCount(geometry);
        const minimum = minimumVertices(geometry);

        for (let index = 0; index < count; index += 1) {
            const point = ring[index] as [number, number];
            const marker = L.marker([point[1], point[0]], { draggable: true, icon: handleIcon("vertex") });
            marker.on("drag", (event: L.LeafletEvent) => {
                const position = (event.target as L.Marker).getLatLng();
                moveVertex(geometry, index, [position.lng, position.lat]);
                redrawSelected();
            });
            marker.on("dragend", () => {
                markDirty();
                render();
            });
            marker.on("click", (event: L.LeafletMouseEvent) => {
                if (!(event.originalEvent && (event.originalEvent.altKey || event.originalEvent.metaKey))) return;
                if (!removeVertex(geometry, index)) {
                    toast.info(closed ? "A room needs at least three corners." : "A wall needs at least two points.");
                    return;
                }
                markDirty();
                render();
            });
            marker.bindTooltip("Drag to move · Alt-click to remove", { direction: "top" });
            handleLayers.addLayer(marker);
        }

        const segments = closed ? count : count - 1;
        for (let index = 0; index < segments; index += 1) {
            const from = ring[index] as [number, number];
            const to = ring[(index + 1) % count] as [number, number];
            const middle = midpoint(from, to);
            const marker = L.marker([middle[1], middle[0]], { draggable: true, icon: handleIcon("midpoint") });
            let inserted = false;
            marker.on("drag", (event: L.LeafletEvent) => {
                const position = (event.target as L.Marker).getLatLng();
                if (!inserted) {
                    insertVertex(geometry, index, [position.lng, position.lat]);
                    inserted = true;
                } else {
                    moveVertex(geometry, index + 1, [position.lng, position.lat]);
                }
                redrawSelected();
            });
            marker.on("dragend", () => {
                markDirty();
                render();
            });
            marker.bindTooltip("Drag to add a point", { direction: "top" });
            handleLayers.addLayer(marker);
        }
    }

    /** Redraw just the selected item's own layer mid-drag, handles untouched. */
    function redrawSelected(): void {
        if (!state.selected) return;
        const item = state.selected.item as { geometry?: Geometry };
        const existing = layerByItem.get(item);
        if (existing) itemLayers.removeLayer(existing);
        if (!item.geometry) return;
        const style = state.selected.type === "room" ? ROOM_STYLE : KIND_STYLE[(item as ElementItem).kind] || (KIND_STYLE.other as { color: string; weight: number });
        const layer = geometryToLayer(item.geometry, { ...style, color: "#d81b60", weight: 4 });
        itemLayers.addLayer(layer);
        layerByItem.set(item, layer);
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

        if (type === "element") host.appendChild(kindAttributeEditor(element));
        if (profileLabels.length) host.appendChild(labelPicker(item as Item));
        host.appendChild(sourceEditor(item as Item));
        host.appendChild(referenceEditor(item as Item));
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

    /** Consumer-local data is namespaced inside `attributes` so it survives a
     * round trip through REData, which rejects unknown top-level keys. */
    function localBag(item: Item): Record<string, unknown> {
        const attributes = (item.attributes = item.attributes || {});
        const existing = attributes[LOCAL_NAMESPACE];
        const bag = (existing && typeof existing === "object" ? existing : {}) as Record<string, unknown>;
        attributes[LOCAL_NAMESPACE] = bag;
        return bag;
    }

    function itemLabels(item: Item): string[] {
        const bag = localBag(item);
        return Array.isArray(bag.labels) ? (bag.labels as string[]) : [];
    }

    function labelPicker(item: Item): HTMLElement {
        const wrap = document.createElement("div");
        wrap.className = "floorplan-form__labels";
        const caption = document.createElement("span");
        caption.textContent = "Labels";
        wrap.appendChild(caption);
        const chosen = new Set(itemLabels(item));
        for (const label of profileLabels) {
            const row = document.createElement("label");
            const box = document.createElement("input");
            box.type = "checkbox";
            box.checked = chosen.has(label.uuid);
            box.addEventListener("change", () => {
                if (box.checked) chosen.add(label.uuid);
                else chosen.delete(label.uuid);
                localBag(item).labels = Array.from(chosen);
                markDirty();
            });
            row.append(box, document.createTextNode(label.name));
            wrap.appendChild(row);
        }
        return wrap;
    }

    /** Inputs for the attribute keys REData publishes for this element's kind. */
    function kindAttributeEditor(element: ElementItem): HTMLElement {
        const wrap = document.createElement("div");
        wrap.className = "floorplan-form__attributes";
        const fields = KIND_ATTRIBUTES[element.kind] || [];
        const attributes = (element.attributes = element.attributes || {});

        if (element.kind === "wall") {
            wrap.appendChild(numberRow("Thickness (m)", element.thickness_meters, (value) => {
                element.thickness_meters = value;
                markDirty();
            }));
        }
        if (element.geometry?.type === "Point") {
            // A point has no shape to carry orientation, so a chair or an
            // appliance is undrawable without this.
            wrap.appendChild(numberRow("Rotation (° from north)", element.rotation_degrees, (value) => {
                element.rotation_degrees = value;
                markDirty();
            }));
        }

        for (const field of fields) {
            const row = document.createElement("label");
            row.className = "floorplan-form__field";
            const caption = document.createElement("span");
            caption.textContent = field.label;
            let input: HTMLInputElement | HTMLSelectElement;
            if (field.options) {
                const select = document.createElement("select");
                for (const option of field.options) {
                    const node = document.createElement("option");
                    node.value = option;
                    node.textContent = option || "— unset —";
                    select.appendChild(node);
                }
                select.value = String(attributes[field.key] ?? "");
                input = select;
            } else {
                const text = document.createElement("input");
                text.type = "text";
                text.value = String(attributes[field.key] ?? "");
                input = text;
            }
            input.addEventListener("change", () => {
                if (input.value) attributes[field.key] = input.value;
                else delete attributes[field.key];
                markDirty();
            });
            row.append(caption, input);
            wrap.appendChild(row);
        }
        return wrap;
    }

    function numberRow(label: string, value: number | null | undefined, onChange: (value: number | null) => void): HTMLElement {
        const wrap = document.createElement("label");
        wrap.className = "floorplan-form__field";
        const caption = document.createElement("span");
        caption.textContent = label;
        const input = document.createElement("input");
        input.type = "number";
        input.step = "any";
        input.value = value === null || value === undefined ? "" : String(value);
        input.addEventListener("change", () => onChange(input.value === "" ? null : Number(input.value)));
        wrap.append(caption, input);
        return wrap;
    }

    function uuidv4(): string {
        return (crypto as Crypto & { randomUUID?: () => string }).randomUUID?.() || `local-${Math.random().toString(36).slice(2)}-${performance.now().toString(36)}`;
    }

    /**
     * Where this item's information came from. Sources live in the plan's
     * pool so ten walls traced from one drawing share one row - picking an
     * existing source reuses it rather than duplicating.
     */
    function sourceEditor(item: Item): HTMLElement {
        const wrap = document.createElement("div");
        wrap.className = "floorplan-form__source";
        const caption = document.createElement("span");
        caption.textContent = "Source";
        wrap.appendChild(caption);

        const pool = (state.doc!.source_pool = state.doc!.source_pool || []);
        const select = document.createElement("select");
        const none = document.createElement("option");
        none.value = "";
        none.textContent = "— none —";
        select.appendChild(none);
        for (const source of pool) {
            const option = document.createElement("option");
            option.value = String(source.uuid);
            option.textContent = String(source.title || source.author || source.url || "source");
            select.appendChild(option);
        }
        select.value = item.source || "";
        select.addEventListener("change", () => {
            item.source = select.value || null;
            markDirty();
        });
        wrap.appendChild(select);

        const add = document.createElement("button");
        add.type = "button";
        add.textContent = "+ New source";
        add.addEventListener("click", () => {
            const title = window.prompt("Where did this come from? (title, e.g. 'HABS sheet 4' or 'measured on site')");
            if (title === null) return;
            const url = window.prompt("Link to it, if there is one (optional)") || "";
            const source = { uuid: uuidv4(), title, url, note: "", author: "", attributes: {} };
            pool.push(source);
            item.source = source.uuid;
            markDirty();
            renderForm();
        });
        wrap.appendChild(add);
        return wrap;
    }

    /** Photos, PDFs and videos evidencing this item, from the plan's pool. */
    function referenceEditor(item: Item): HTMLElement {
        const wrap = document.createElement("div");
        wrap.className = "floorplan-form__references";
        const caption = document.createElement("span");
        caption.textContent = "References";
        wrap.appendChild(caption);

        const pool = (state.doc!.reference_pool = state.doc!.reference_pool || []);
        const linked = new Set(item.references || []);
        for (const reference of pool) {
            const row = document.createElement("label");
            const box = document.createElement("input");
            box.type = "checkbox";
            box.checked = linked.has(String(reference.uuid));
            box.addEventListener("change", () => {
                if (box.checked) linked.add(String(reference.uuid));
                else linked.delete(String(reference.uuid));
                item.references = Array.from(linked);
                markDirty();
            });
            const text = document.createElement("span");
            text.textContent = String(reference.title || reference.url || reference.kind || "reference");
            row.append(box, text);
            wrap.appendChild(row);
        }

        if (pinPhotos.length) {
            const gallery = document.createElement("div");
            gallery.className = "floorplan-form__photo-picker";
            for (const photo of pinPhotos.slice(0, 24)) {
                const button = document.createElement("button");
                button.type = "button";
                button.title = photo.caption || "Attach this photo as evidence";
                const thumb = document.createElement("img");
                thumb.src = photo.url;
                thumb.alt = photo.caption || "";
                thumb.loading = "lazy";
                button.appendChild(thumb);
                button.addEventListener("click", () => {
                    // One pool row per photo: attaching the same photo to a
                    // wall, its door and the door's lock must not make three.
                    const existing = pool.find((entry) => entry.image_uuid === photo.uuid);
                    const reference = existing || { uuid: uuidv4(), kind: "photo", title: photo.caption, url: "", description: "", attributes: {}, image_uuid: photo.uuid };
                    if (!existing) pool.push(reference);
                    linked.add(String(reference.uuid));
                    item.references = Array.from(linked);
                    markDirty();
                    renderForm();
                });
                gallery.appendChild(button);
            }
            wrap.appendChild(gallery);
        }

        const add = document.createElement("button");
        add.type = "button";
        add.textContent = "+ Reference by URL";
        add.addEventListener("click", () => {
            const url = window.prompt("URL of a photo, PDF, or video showing this");
            if (!url) return;
            const title = window.prompt("Title (optional)") || "";
            const lowered = url.toLowerCase();
            const kind = lowered.endsWith(".pdf") ? "pdf" : /\.(mp4|mov|webm)$/.test(lowered) ? "video" : /\.(jpg|jpeg|png|gif|webp)$/.test(lowered) ? "photo" : "link";
            const reference = { uuid: uuidv4(), kind: kind === "link" ? "other" : kind, title, url, description: "", attributes: {} };
            pool.push(reference);
            linked.add(reference.uuid);
            item.references = Array.from(linked);
            markDirty();
            renderForm();
        });
        wrap.appendChild(add);
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
    const POINT_TOOLS = new Set(["door", "window", "stair", "fixture", "furniture", "column", "other"]);

    function setTool(tool: string): void {
        state.tool = tool;
        state.drawing = null;
        state.selected = null;
        document.querySelectorAll<HTMLButtonElement>("#floorplan-tools button").forEach((button) => {
            button.classList.toggle("is-active", button.dataset.tool === tool);
        });
        mapEl.classList.toggle("is-drawing", tool !== "select");
        // Finishing a shape is a double-click; leaving zoom bound to it would
        // zoom the map every time somebody closes a room.
        if (tool === "select") map.doubleClickZoom.enable();
        else map.doubleClickZoom.disable();
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
        // A double-click fires two clicks first, so the finishing point has
        // already been added twice - drop the repeat rather than storing a
        // zero-length segment.
        const points = dropTrailingDuplicates(drawing.latlngs);
        state.drawing = null;
        if (POLYGON_TOOLS.has(state.tool) && points.length >= 3) {
            const geometry = { type: "Polygon", coordinates: [closeRing(points)] };
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
        renderVersionPicker();
        render();
    }

    /** Switch between the building's dated plan versions. */
    function renderVersionPicker(): void {
        const host = document.getElementById("floorplan-versions");
        if (!host) return;
        const versions = state.doc?.versions || [];
        host.hidden = versions.length < 2;
        if (host.hidden) return;
        host.innerHTML = "";
        const caption = document.createElement("span");
        caption.textContent = "Version";
        const select = document.createElement("select");
        for (const version of versions) {
            const option = document.createElement("option");
            option.value = version.uuid;
            option.textContent = `${version.name || "Floorplan"} · ${version.valid_from || "original"}`;
            select.appendChild(option);
        }
        select.value = String(state.doc?.uuid || "");
        select.addEventListener("change", () => {
            if (state.dirty && !window.confirm("Discard unsaved changes and open the other version?")) {
                select.value = String(state.doc?.uuid || "");
                return;
            }
            void load(select.value);
        });
        host.append(caption, select);
    }

    async function load(versionUuid = ""): Promise<void> {
        const params = new URLSearchParams(window.location.search);
        let url = jsonUrl;
        if (versionUuid) url = `${jsonUrl}?version=${encodeURIComponent(versionUuid)}`;
        else if (params.get("date")) url = `${jsonUrl}?date=${encodeURIComponent(params.get("date") as string)}`;
        const response = await fetch(url, { headers: { Accept: "application/json" } });
        state.doc = response.status === 204 ? emptyDoc() : ((await response.json()) as FloorplanDocument);
        if (state.doc.origin === "community") {
            const banner = document.getElementById("floorplan-origin-banner");
            if (banner) {
                banner.textContent = "This is the community floorplan for this place. Your edits are shared with everyone who can see its wiki.";
                banner.hidden = false;
            }
        }
        if (state.doc.origin === "redata") {
            const banner = document.getElementById("floorplan-origin-banner");
            if (banner) banner.hidden = false;
        }
        const nameInput = document.getElementById("floorplan-name") as HTMLInputElement | null;
        if (nameInput) nameInput.value = state.doc.name || "";
        const dateInput = document.getElementById("floorplan-valid-from") as HTMLInputElement | null;
        if (dateInput) dateInput.value = state.doc.valid_from || "";
        state.floorIndex = 0;
        state.selected = null;
        state.dirty = false;
        const first = (state.doc.floors || []).find((floor) => floor.geometry || (floor.rooms || []).length || (floor.elements || []).length);
        fitTo(first);
        renderVersionPicker();
        render();
    }

    /** Zoom to everything drawn on a floor - an outline is not required. */
    function fitTo(floor: FloorItem | undefined): void {
        if (!floor) return;
        const geometries: Geometry[] = [floor.geometry || null, ...(floor.rooms || []).map((room) => room.geometry || null), ...(floor.elements || []).map((element) => element.geometry || null)];
        let bounds: L.LatLngBounds | null = null;
        for (const geometry of geometries) {
            if (!geometry) continue;
            const layer = geometryToLayer(geometry, { weight: 1 });
            const point = "getBounds" in (layer as L.Polygon) ? null : (layer as L.CircleMarker).getLatLng();
            const own = point ? L.latLngBounds(point, point) : (layer as L.Polygon).getBounds();
            if (!own.isValid()) continue;
            bounds = bounds ? bounds.extend(own) : own;
        }
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
    document.getElementById("floorplan-publish")?.addEventListener("click", () => {
        void (async () => {
            if (!state.doc?.uuid) {
                toast.info("Save the floorplan before publishing it.");
                return;
            }
            if (state.dirty && !window.confirm("Publish the last saved version? Your unsaved changes are not included.")) return;
            const response = await fetch(publishUrl, {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                body: JSON.stringify({ uuid: state.doc.uuid }),
            });
            const body = await response.json().catch(() => ({}));
            if (!response.ok || !body.ok) {
                toast.warning(body.error || "Could not publish this floorplan.");
                return;
            }
            toast.success("Published to the community wiki.");
        })();
    });
    document.getElementById("floorplan-save-version")?.addEventListener("click", () => {
        if (!state.doc) return;
        // Dropping the plan uuid makes the save create a second version and
        // leave the loaded one standing - how a building's layout change
        // (a renovation, a fire) is recorded without losing what came before.
        delete state.doc.uuid;
        void save();
    });
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
