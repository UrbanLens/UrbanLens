/**
 * A Leaflet map of geolocated items paired with a card grid: clicking or drag-selecting markers and ticking cards
 * build one selection, which the bulk toolbar acts on. Memories > Locations and Memories > Visits use it.
 */

declare const L: typeof import("leaflet");

export type ItemId = string | number;

export interface PinSelectItem {
    latitude: number;
    longitude: number;
}

export interface PinSelectMapOptions<T extends PinSelectItem> {
    dataUrl: string;
    /** The key in the JSON response holding the array. */
    itemsKey: string;
    /** Validates one raw item from that array; null skips it. */
    parse: (raw: unknown) => T | null;
    idOf: (item: T) => ItemId;
    icon: (item: T, selected: boolean) => L.DivIcon;
    tooltip?: (item: T) => string;
    cardEl: (id: ItemId) => HTMLElement | null;
    /** Root of the delegated checkbox and hover listeners, so they survive htmx swaps and pagination. */
    wrapEl?: HTMLElement | null;
    checkboxSelector?: string;
    /** The ``dataset`` key on a checkbox holding its item's id. */
    checkboxIdAttr?: string;
    /** Pairs hover between a card and its marker (``.is-hovered`` on both). */
    cardSelector?: string;
    layersPanelId?: string;
    selectToggleBtnId?: string;
    /** The bulk toolbar's namespace. */
    namespace: string;
    bulkActions: Record<string, (ids: ItemId[]) => Promise<unknown>>;
    onMarkerClick?: (item: T) => void;
    /** A body event that reloads the markers. */
    refreshEvent?: string;
}

export interface PinSelectMap {
    toggleSelection(id: ItemId): void;
    clearSelection(): void;
    reload(): Promise<void>;
    getSelected(): ItemId[];
}

/** Checkbox ids are opaque; one that looks numeric matches a numeric id in the map data. */
export function parseItemId(raw: string | undefined): ItemId | null {
    if (!raw) return null;
    return /^-?\d+$/.test(raw) ? Number.parseInt(raw, 10) : raw;
}

export interface BulkOutcome {
    processed?: number;
    failed?: number;
    skipped?: number;
    requested?: number;
}

/**
 * Toast a per-row bulk endpoint's counts (``services.core.bulk_outcome``). Failed and skipped rows get different
 * words: "already handled" and "went wrong, try again" ask different things of the user.
 */
export function reportBulkOutcome(data: BulkOutcome, verb: string, noun: string): void {
    const toastr = window.toastr;
    if (!toastr) return;
    const done = data.processed ?? 0;
    const failed = data.failed ?? 0;
    const skipped = data.skipped ?? 0;
    const requested = data.requested ?? done + failed + skipped;
    const nouns = (n: number): string => `${n} ${noun}${n === 1 ? "" : "s"}`;
    if (requested === 0) toastr.info("There was nothing to do.");
    else if (failed && !done) toastr.error(`Something went wrong with ${nouns(failed)}. Please try again.`);
    else if (failed) toastr.warning(`${verb} ${done} of ${nouns(requested)}. ${failed} went wrong - please try again.`);
    else if (skipped) toastr.warning(`${verb} ${done} of ${nouns(requested)}. The rest had already been handled.`);
    else toastr.success(`${verb} ${nouns(done)}.`);
}

/** Read a bulk endpoint's JSON counts, rejecting on a failed response. */
export async function bulkOutcomeOf(response: Response): Promise<BulkOutcome> {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data: unknown = await response.json();
    const count = (key: string): number | undefined => {
        const value: unknown = data && typeof data === "object" && key in data ? Reflect.get(data, key) : undefined;
        return typeof value === "number" ? value : undefined;
    };
    return { processed: count("processed"), failed: count("failed"), skipped: count("skipped"), requested: count("requested") };
}

export function createPinSelectMap<T extends PinSelectItem>(mapEl: HTMLElement, opts: PinSelectMapOptions<T>): PinSelectMap {
    const refreshEvent = opts.refreshEvent ?? "refreshQueue";
    const map = L.map(mapEl, { attributionControl: false }).setView([20, 0], 2);
    window.MapLayers.create(map, { root: opts.layersPanelId ? document.getElementById(opts.layersPanelId) : null, onAttribution: window.MapLayers.setAttribution });

    const selected = new Set<ItemId>();
    const markers = new Map<ItemId, L.Marker>();
    const items = new Map<ItemId, T>();
    const cardIds = new WeakMap<Element, ItemId>();

    const syncMarker = (id: ItemId): void => {
        const item = items.get(id);
        if (item) markers.get(id)?.setIcon(opts.icon(item, selected.has(id)));
    };
    const syncCard = (id: ItemId): void => {
        const card = opts.cardEl(id);
        if (!card) return;
        card.classList.toggle("is-selected", selected.has(id));
        const checkbox = opts.checkboxSelector ? card.querySelector(opts.checkboxSelector) : null;
        if (checkbox instanceof HTMLInputElement) checkbox.checked = selected.has(id);
    };
    const setHover = (id: ItemId, on: boolean): void => {
        markers.get(id)?.getElement()?.classList.toggle("is-hovered", on);
        opts.cardEl(id)?.classList.toggle("is-hovered", on);
    };

    const runBulkAction = (action: string): void => {
        const ids = [...selected];
        const run = opts.bulkActions[action];
        if (!ids.length || !run) return;
        run(ids)
            .then(() => {
                clearSelection();
                document.body.dispatchEvent(new CustomEvent(refreshEvent));
            })
            .catch(() => window.toastr?.error("Something went wrong. Please try again."));
    };
    const syncToolbar = (): void => {
        const actions: Record<string, () => void> = {};
        if (selected.size) {
            for (const action of Object.keys(opts.bulkActions)) actions[action] = () => runBulkAction(action);
            actions.deselect = clearSelection;
        }
        window.ulBulkToolbar?.sync(opts.namespace, selected.size, actions);
    };
    const toggleSelection = (id: ItemId): void => {
        if (selected.has(id)) selected.delete(id);
        else selected.add(id);
        syncMarker(id);
        syncCard(id);
        syncToolbar();
    };
    function clearSelection(): void {
        const ids = [...selected];
        selected.clear();
        for (const id of ids) {
            syncMarker(id);
            syncCard(id);
        }
        syncToolbar();
    }

    const reload = async (): Promise<void> => {
        try {
            const response = await fetch(opts.dataUrl);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data: unknown = await response.json();
            const raw: unknown = data && typeof data === "object" && opts.itemsKey in data ? Reflect.get(data, opts.itemsKey) : [];
            for (const marker of markers.values()) map.removeLayer(marker);
            markers.clear();
            items.clear();
            const bounds: L.LatLngTuple[] = [];
            for (const entry of Array.isArray(raw) ? raw : []) {
                const item = opts.parse(entry);
                if (!item) continue;
                const id = opts.idOf(item);
                items.set(id, item);
                const marker = L.marker([item.latitude, item.longitude], { icon: opts.icon(item, selected.has(id)) }).addTo(map);
                if (opts.tooltip) marker.bindTooltip(opts.tooltip(item));
                marker.on("click", () => {
                    toggleSelection(id);
                    opts.onMarkerClick?.(item);
                });
                marker.on("mouseover", () => setHover(id, true));
                marker.on("mouseout", () => setHover(id, false));
                markers.set(id, marker);
                const card = opts.cardEl(id);
                if (card) cardIds.set(card, id);
                bounds.push([item.latitude, item.longitude]);
            }
            // Items handled elsewhere, or off this page, leave the selection.
            for (const id of [...selected]) if (!markers.has(id)) selected.delete(id);
            syncToolbar();
            const [only] = bounds;
            if (bounds.length === 1 && only) map.setView(only, 14);
            else if (bounds.length > 1) map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 });
        } catch {
            window.toastr?.error("Could not load pins for the map. Refresh to try again.");
        }
    };

    // Select mode trades panning for rectangle drag-select, set up front so there is no race with Leaflet's own drag.
    let selectMode = false;
    const toggleBtn = opts.selectToggleBtnId ? document.getElementById(opts.selectToggleBtnId) : null;
    toggleBtn?.addEventListener("click", () => {
        selectMode = !selectMode;
        if (selectMode) map.dragging.disable();
        else map.dragging.enable();
        toggleBtn.classList.toggle("active", selectMode);
        mapEl.classList.toggle("select-mode", selectMode);
    });

    let dragRect: L.Rectangle | null = null;
    map.getContainer().addEventListener("mousedown", (down) => {
        if (!selectMode || down.button !== 0) return;
        const start = map.mouseEventToLatLng(down);
        let dragging = false;
        const onMove = (move: MouseEvent): void => {
            // A plain click on empty map is not a zero-size selection.
            if (!dragging && Math.hypot(move.clientX - down.clientX, move.clientY - down.clientY) < 6) return;
            dragging = true;
            if (dragRect) map.removeLayer(dragRect);
            dragRect = L.rectangle(L.latLngBounds(start, map.mouseEventToLatLng(move)), { color: "#1E88E5", weight: 2, fillOpacity: 0.08, dashArray: "4 4", interactive: false }).addTo(map);
        };
        const onUp = (up: MouseEvent): void => {
            document.removeEventListener("mousemove", onMove);
            if (dragRect) {
                map.removeLayer(dragRect);
                dragRect = null;
            }
            if (!dragging) return;
            const box = L.latLngBounds(start, map.mouseEventToLatLng(up));
            for (const [id, marker] of markers) if (!selected.has(id) && box.contains(marker.getLatLng())) toggleSelection(id);
        };
        document.addEventListener("mousemove", onMove);
        document.addEventListener("mouseup", onUp, { once: true });
    });

    // A single-item action (via HX-Trigger) or a bulk one above.
    document.body.addEventListener(refreshEvent, () => void reload());
    void reload();

    const wrap = opts.wrapEl;
    const checkboxSelector = opts.checkboxSelector;
    if (wrap && checkboxSelector) {
        wrap.addEventListener("change", (event) => {
            const target = event.target;
            if (!(target instanceof HTMLInputElement) || !target.matches(checkboxSelector)) return;
            const id = parseItemId(target.dataset[opts.checkboxIdAttr ?? "id"]);
            if (id !== null) toggleSelection(id);
        });
    }
    const cardSelector = opts.cardSelector;
    if (wrap && cardSelector) {
        // mouseover and mouseout bubble, so entering and leaving a card is tracked by hand.
        let hovered: Element | null = null;
        wrap.addEventListener("mouseover", (event) => {
            const card = event.target instanceof Element ? event.target.closest(cardSelector) : null;
            if (card === hovered) return;
            const previous = hovered ? cardIds.get(hovered) : undefined;
            if (previous !== undefined) setHover(previous, false);
            hovered = card;
            const next = card ? cardIds.get(card) : undefined;
            if (next !== undefined) setHover(next, true);
        });
        wrap.addEventListener("mouseout", (event) => {
            if (!hovered) return;
            if (event.relatedTarget instanceof Node && hovered.contains(event.relatedTarget)) return;
            const id = cardIds.get(hovered);
            if (id !== undefined) setHover(id, false);
            hovered = null;
        });
    }

    return { toggleSelection, clearSelection, reload, getSelected: () => [...selected] };
}
