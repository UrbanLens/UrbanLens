/**
 * The saved-filter create/edit form (``partials/pin_lists/_saved_filter_fields.html``): in a dialog on the
 * Organize page's Filters tab and the pin-list page, and inline on a filter's own page.
 *
 * Its URLs arrive on ``#saved-filter-config`` (``partials/pin_lists/_saved_filter_config.html``). The dialog's body is
 * swapped in by htmx on every open, so every handler is delegated, and the region map and label picker are
 * built when the dialog opens rather than when its body arrives: a closed dialog has no size to measure.
 */

import type {} from "leaflet-draw";

import { getCsrfToken } from "./csrf";
import { confirmAction, toast } from "./dialogs";
import { fetchJson, fetchText } from "./fetch-json";
import { htmxDetail } from "./htmx-events";
import type { LabelGroup } from "./label-picker";
import type { RegionGeoJson } from "./region-delete";

declare const L: typeof import("leaflet");

type RegionMode = "include" | "exclude";

interface RegionResult {
    display_name: string;
    geojson: RegionGeoJson;
}

const PLACEHOLDER_UUID = "00000000-0000-0000-0000-000000000000";
const DEFAULT_CENTER: [number, number] = [39.8283, -98.5795];
const SUGGEST_DELAY_MS = 400;
/** Leaflet.draw can still be loading when the Filters tab opens a dialog; how many 100ms retries to allow. */
const DRAW_LOAD_RETRIES = 40;

const REGION_STYLES: Record<RegionMode, L.PathOptions> = {
    include: { color: "#2e7d32", fillColor: "#2e7d32", fillOpacity: 0.25, weight: 2 },
    exclude: { color: "#c62828", fillColor: "#c62828", fillOpacity: 0.25, weight: 2 },
};

function config(): DOMStringMap {
    return document.getElementById("saved-filter-config")?.dataset ?? {};
}

function form(): HTMLFormElement | null {
    const el = document.getElementById("saved-filter-form");
    return el instanceof HTMLFormElement ? el : null;
}

function input(id: string): HTMLInputElement | null {
    const el = document.getElementById(id);
    return el instanceof HTMLInputElement ? el : null;
}

function errorText(err: unknown, fallback: string): string {
    return err instanceof Error && err.message ? err.message : fallback;
}

/** Where a save or delete leaves the viewer: the page named by the dialog, or this one again. */
function afterSaveOrDelete(): void {
    const redirect = document.getElementById("saved-filter-form-dialog")?.dataset.successRedirect;
    if (redirect) window.location.href = redirect;
    else window.location.reload();
}

// -- Save and delete

async function submit(target: HTMLFormElement): Promise<void> {
    try {
        await fetchText(target.dataset.action ?? "", {
            method: "POST",
            headers: { "X-CSRFToken": getCsrfToken() },
            body: new FormData(target),
            reportsItsOwnErrors: true,
        });
    } catch (err) {
        toast.error(errorText(err, "Could not save this filter."));
        return;
    }
    afterSaveOrDelete();
}

export async function deleteSavedFilter(uuid: string, name: string): Promise<void> {
    const ok = await confirmAction({
        title: `Delete the saved filter "${name}"?`,
        message: "You can restore it from Settings → Undo History.",
        confirmLabel: "Delete",
    });
    if (!ok) return;
    const url = (config().deleteUrlTemplate ?? "").replace(PLACEHOLDER_UUID, uuid);
    try {
        await fetchText(url, { method: "POST", headers: { "X-CSRFToken": getCsrfToken() }, reportsItsOwnErrors: true });
    } catch {
        toast.error("Could not delete this filter.");
        return;
    }
    afterSaveOrDelete();
}

// -- Visited? chips: a second click on the chosen one clears it, like the map's filter sidebar

function syncVisits(value: string): void {
    const hidden = input("sf-visits-hidden");
    if (hidden) hidden.value = value;
    const reset = document.getElementById("sf-visits-reset");
    if (reset) reset.style.display = value ? "" : "none";
    const dates = document.getElementById("sf-visits-dates");
    if (dates) {
        dates.style.display = value === "yes" ? "" : "none";
        if (value !== "yes") dates.querySelectorAll<HTMLInputElement>("input[type=date]").forEach((el) => (el.value = ""));
    }
    for (const id of ["sf-visits-chip-yes", "sf-visits-chip-no"]) {
        const radio = document.getElementById(id)?.querySelector<HTMLInputElement>("input[type=radio]");
        if (radio) radio.checked = radio.value === value;
    }
}

function initVisits(): void {
    const hidden = input("sf-visits-hidden");
    if (!hidden) return;
    syncVisits(hidden.value);
    for (const id of ["sf-visits-chip-yes", "sf-visits-chip-no"]) {
        const chip = document.getElementById(id);
        const radio = chip?.querySelector<HTMLInputElement>("input[type=radio]");
        if (!chip || !radio || chip.dataset.visitsBound) continue;
        chip.dataset.visitsBound = "1";
        chip.addEventListener("mousedown", () => (chip.dataset.wasChecked = String(radio.checked)));
        chip.addEventListener("click", () => syncVisits(chip.dataset.wasChecked === "true" ? "" : radio.value));
    }
}

// -- Name suggestion: the server's summary of the criteria, until the viewer names it themselves

let nameEdited = false;
let suggestTimer: number | undefined;
let suggestToken = 0;

async function suggestName(): Promise<void> {
    const target = form();
    const name = input("saved-filter-name");
    const url = config().suggestNameUrl;
    if (!target || !name || !url || nameEdited) return;
    const token = ++suggestToken;
    let data: { name?: string } | null;
    try {
        data = await fetchJson<{ name?: string }>(url, {
            method: "POST",
            headers: { "X-CSRFToken": getCsrfToken() },
            body: new FormData(target),
            reportsItsOwnErrors: true,
        });
    } catch {
        return;
    }
    if (token !== suggestToken || nameEdited) return;
    if (data?.name) name.value = data.name;
}

function scheduleNameSuggestion(): void {
    if (nameEdited) return;
    window.clearTimeout(suggestTimer);
    suggestTimer = window.setTimeout(() => void suggestName(), SUGGEST_DELAY_MS);
}

function startNaming(): void {
    // An existing filter's name is its own; only a blank one takes suggestions.
    nameEdited = !!input("saved-filter-name")?.value.trim();
}

// -- Region map: include and exclude polygons, drawn or found by place name

let regionMap: L.Map | null = null;
let regionLayers: L.FeatureGroup | null = null;
let regionMode: RegionMode = "include";

function setRegionMode(mode: RegionMode): void {
    regionMode = mode;
    document.querySelectorAll<HTMLElement>(".saved-filter-region-mode-btn").forEach((btn) => {
        btn.classList.toggle("saved-filter-region-mode-btn--active", btn.dataset.regionMode === mode);
    });
}

/** Each drawn region's mode; Leaflet.draw's edit tool keeps the same layer objects. */
const regionModes = new WeakMap<L.Layer, RegionMode>();

function styleRegion(layer: L.Layer, mode: RegionMode): void {
    regionModes.set(layer, mode);
    if (layer instanceof L.Path) layer.setStyle(REGION_STYLES[mode]);
}

/** Write the drawn regions into the form's hidden inputs, as the two MultiPolygons the server stores. */
function saveRegions(): void {
    if (!regionLayers) return;
    const include: GeoJSON.Position[][][] = [];
    const exclude: GeoJSON.Position[][][] = [];
    regionLayers.eachLayer((layer) => {
        if (!(layer instanceof L.Polygon)) return;
        const geometry = layer.toGeoJSON().geometry;
        const into = regionModes.get(layer) === "exclude" ? exclude : include;
        if (geometry.type === "Polygon") into.push(geometry.coordinates);
        else if (geometry.type === "MultiPolygon") into.push(...geometry.coordinates);
    });
    const encode = (coordinates: GeoJSON.Position[][][]): string => (coordinates.length ? JSON.stringify({ type: "MultiPolygon", coordinates }) : "");
    const includeInput = input("saved-filter-include-regions");
    if (includeInput) includeInput.value = encode(include);
    const excludeInput = input("saved-filter-exclude-regions");
    if (excludeInput) excludeInput.value = encode(exclude);
    scheduleNameSuggestion();
    // Setting .value fires nothing, and the filter page's live preview listens for change.
    form()?.dispatchEvent(new Event("change", { bubbles: true }));
}

function addRegion(geojson: RegionGeoJson | null, mode: RegionMode): void {
    const group = regionLayers;
    if (!group || !window.RegionDelete) return;
    const layers = window.RegionDelete.polygonParts(geojson).flatMap((part) => L.geoJSON(part).getLayers());
    for (const layer of layers) {
        styleRegion(layer, mode);
        group.addLayer(layer);
    }
    if (layers.length && regionMap) regionMap.fitBounds(group.getBounds());
}

function parseStored(id: string): RegionGeoJson | null {
    try {
        const value = input(id)?.value;
        // polygonParts drops anything that is not a polygon.
        return value ? JSON.parse(value) : null;
    } catch {
        return null;
    }
}

function initRegionMap(attempt = 0): void {
    const mapEl = document.getElementById("saved-filter-region-map");
    if (!mapEl || regionMap) return;
    if (typeof L === "undefined" || typeof L.Draw === "undefined" || !window.RegionDelete || !window.MapLayers) {
        if (attempt < DRAW_LOAD_RETRIES) window.setTimeout(() => initRegionMap(attempt + 1), 100);
        return;
    }
    const center: [number, number] = [Number(config().mapCenterLat) || DEFAULT_CENTER[0], Number(config().mapCenterLng) || DEFAULT_CENTER[1]];
    const map = L.map(mapEl, { attributionControl: false }).setView(center, 4);
    regionMap = map;
    window.MapLayers.tileLayer("street").addTo(map);
    const drawn = new L.FeatureGroup().addTo(map);
    regionLayers = drawn;
    const include = parseStored("saved-filter-include-regions");
    if (include) addRegion(include, "include");
    const exclude = parseStored("saved-filter-exclude-regions");
    if (exclude) addRegion(exclude, "exclude");

    map.addControl(
        new L.Control.Draw({
            draw: { polygon: { shapeOptions: REGION_STYLES.include }, marker: false, circle: false, circlemarker: false, rectangle: false, polyline: false },
            edit: { featureGroup: drawn, remove: false },
        }),
    );
    window.RegionDelete.add(map, drawn, saveRegions);
    map.on(L.Draw.Event.CREATED, (event) => {
        if (!("layer" in event) || !(event.layer instanceof L.Layer)) return;
        styleRegion(event.layer, regionMode);
        drawn.addLayer(event.layer);
        saveRegions();
    });
    map.on(L.Draw.Event.EDITED, saveRegions);
    setRegionMode("include");
    window.setTimeout(() => map.invalidateSize(), 0);
}

function showRegionResults(results: RegionResult[]): void {
    const list = document.getElementById("saved-filter-region-search-results");
    if (!list) return;
    list.replaceChildren(
        ...results.map((result) => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "saved-filter-region-result";
            btn.textContent = result.display_name;
            btn.addEventListener("click", () => {
                addRegion(result.geojson, regionMode);
                saveRegions();
                list.hidden = true;
            });
            return btn;
        }),
    );
    list.hidden = false;
}

async function searchRegion(): Promise<void> {
    const query = input("saved-filter-region-search-input")?.value.trim() ?? "";
    const list = document.getElementById("saved-filter-region-search-results");
    const message = document.getElementById("saved-filter-region-search-message");
    if (list) {
        list.hidden = true;
        list.replaceChildren();
    }
    if (message) message.hidden = true;
    const url = config().regionSearchUrl;
    if (!query || !url) return;
    const say = (text: string): void => {
        if (!message) return;
        message.textContent = text;
        message.hidden = false;
    };
    let results: RegionResult[];
    try {
        results = (await fetchJson<{ results?: RegionResult[] }>(`${url}?q=${encodeURIComponent(query)}`, { reportsItsOwnErrors: true }))?.results ?? [];
    } catch {
        say("Could not search for that place right now.");
        return;
    }
    if (!results.length) {
        say("No area boundary found for that place - try a broader name, like a city or neighborhood, or draw the region manually.");
        return;
    }
    const only = results.length === 1 ? results[0] : undefined;
    if (only) {
        addRegion(only.geojson, regionMode);
        saveRegions();
        return;
    }
    showRegionResults(results);
}

// -- Label picker: the map sidebar's engine (label-picker.ts, from the core bundle) on the sf-* markup

function isLabelGroups(value: unknown): value is LabelGroup[] {
    return (
        Array.isArray(value) &&
        value.every((g: unknown) => typeof g === "object" && g !== null && "op" in g && (g.op === "and" || g.op === "or" || g.op === "not") && "ids" in g && Array.isArray(g.ids) && g.ids.every((id: unknown) => typeof id === "number"))
    );
}

/** The stored structured groups when there are any, else the flat sets the template marks on the buttons. */
function initialGroups(root: HTMLElement): LabelGroup[] {
    let stored: unknown = null;
    try {
        stored = JSON.parse(root.dataset.initialGroups || "null");
    } catch {
        stored = null;
    }
    if (isLabelGroups(stored) && stored.length) return stored;
    const incl: number[] = [];
    const excl: number[] = [];
    root.querySelectorAll<HTMLElement>(".fp-label-avail").forEach((btn) => {
        if (btn.dataset.selectedMode === "incl") incl.push(Number(btn.dataset.labelId));
        else if (btn.dataset.selectedMode === "excl") excl.push(Number(btn.dataset.labelId));
    });
    const groups: LabelGroup[] = [];
    if (incl.length) groups.push({ op: "and", ids: incl });
    if (excl.length) groups.push({ op: "not", ids: excl });
    return groups;
}

function initLabelPicker(): void {
    const root = document.getElementById("sf-label-picker");
    const factory = window.UrbanLensLabelPicker?.createFilterPicker;
    // A dialog body is fresh on every open; the marker only stops a second init on the inline page.
    if (!root || !factory || root.dataset.pickerInit === "1") return;
    const el = (id: string): HTMLElement | null => document.getElementById(id);
    const inputEl = (id: string): HTMLInputElement | null => {
        const found = el(id);
        return found instanceof HTMLInputElement ? found : null;
    };
    const list = el("sf-label-list");
    const selected = el("sf-label-selected");
    const colIncl = el("sf-label-col-incl");
    const colExcl = el("sf-label-col-excl");
    const inclChips = el("sf-label-incl-chips");
    const exclChips = el("sf-label-excl-chips");
    if (!list || !selected || !colIncl || !colExcl || !inclChips || !exclChips) return;
    root.dataset.pickerInit = "1";
    const picker = factory({
        els: {
            list,
            selected,
            colIncl,
            colExcl,
            inclChips,
            exclChips,
            modeBtn: el("sf-incl-mode-btn"),
            groupsInput: inputEl("sf-label-groups"),
            formulaBar: inputEl("sf-label-formula"),
            formulaSuggestions: el("sf-formula-suggestions"),
            formulaErrors: el("sf-formula-errors"),
            formulaDisplay: el("sf-formula-display"),
            formulaDisplayText: el("sf-formula-display-text"),
            formulaDisplayClear: el("sf-formula-display-clear"),
            kindTabs: el("sf-label-kind-tabs"),
        },
        onChange: () => root.dispatchEvent(new Event("change", { bubbles: true })),
        // The legacy tags/exclude_tags fields, mirrored from the structured groups.
        onSerialize: (inclIds, exclIds) => {
            root.querySelectorAll("input[data-sf-label-input]").forEach((node) => node.remove());
            const addHidden = (name: string, id: string): void => {
                const box = document.createElement("input");
                box.type = "checkbox";
                box.hidden = true;
                box.checked = true;
                box.name = name;
                box.value = id;
                box.dataset.sfLabelInput = "1";
                root.appendChild(box);
            };
            inclIds.forEach((id) => addHidden("tags", id));
            exclIds.forEach((id) => addHidden("exclude_tags", id));
        },
    });
    const groups = initialGroups(root);
    if (groups.length) picker.applyGroups(groups);
}

// -- Wiring

function initFields(): void {
    initRegionMap();
    initLabelPicker();
    initVisits();
    startNaming();
}

/** Show the dialog htmx just filled, then build what needs its size. */
export function openSavedFilterDialog(): void {
    const dialog = document.getElementById("saved-filter-form-dialog");
    if (dialog instanceof HTMLDialogElement && !dialog.open) dialog.showModal();
    initFields();
}

function onClick(event: MouseEvent): void {
    const target = event.target instanceof Element ? event.target : null;
    const mode = target?.closest<HTMLElement>(".saved-filter-region-mode-btn[data-region-mode]");
    if (mode) {
        setRegionMode(mode.dataset.regionMode === "exclude" ? "exclude" : "include");
        return;
    }
    const control = target?.closest<HTMLElement>("[data-saved-filter-action]");
    switch (control?.dataset.savedFilterAction) {
        case "reset-visits":
            syncVisits("");
            break;
        case "search-region":
            void searchRegion();
            break;
    }
}

/** Captured, so the card link a Filters-tab delete sits in never sees the click. */
function onDeleteClick(event: MouseEvent): void {
    const control = event.target instanceof Element ? event.target.closest<HTMLElement>('[data-saved-filter-action="delete"]') : null;
    if (!control) return;
    event.preventDefault();
    event.stopPropagation();
    void deleteSavedFilter(control.dataset.filterUuid ?? "", control.dataset.filterName ?? "");
}

/** The Filters tab's search box narrows its cards by name. */
function filterCards(query: string): void {
    const q = query.trim().toLowerCase();
    document.querySelectorAll<HTMLElement>("#saved-filters-grid .saved-filter-card").forEach((card) => {
        card.style.display = !q || (card.dataset.search ?? "").includes(q) ? "" : "none";
    });
}

let installed = false;

/**
 * Wire the form wherever it appears. *inline* is for a filter's own page, where the form is part of the page
 * rather than a dialog to open.
 */
export function installSavedFilterForm({ inline = false }: { inline?: boolean } = {}): void {
    if (!installed) {
        installed = true;
        document.addEventListener("click", onClick);
        document.addEventListener("click", onDeleteClick, true);
        document.addEventListener("submit", (event) => {
            if (!(event.target instanceof HTMLFormElement) || event.target.id !== "saved-filter-form") return;
            event.preventDefault();
            void submit(event.target);
        });
        document.addEventListener("keydown", (event) => {
            if (event.key !== "Enter" || !(event.target instanceof HTMLElement) || event.target.id !== "saved-filter-region-search-input") return;
            event.preventDefault();
            void searchRegion();
        });
        document.body.addEventListener("input", (event) => {
            if (event.target instanceof HTMLInputElement && event.target.id === "saved-filters-search") filterCards(event.target.value);
            if (!(event.target instanceof HTMLElement) || !event.target.closest("#saved-filter-form")) return;
            if (event.target.id === "saved-filter-name") {
                nameEdited = event.target instanceof HTMLInputElement && event.target.value.trim().length > 0;
                return;
            }
            scheduleNameSuggestion();
        });
        document.body.addEventListener("change", (event) => {
            if (!(event.target instanceof HTMLElement) || !event.target.closest("#saved-filter-form") || event.target.id === "saved-filter-name") return;
            scheduleNameSuggestion();
        });
        // A new body brings a new map container; the old map went with the old one.
        document.body.addEventListener("htmx:afterSwap", (event) => {
            if (htmxDetail(event).target?.id !== "saved-filter-form-dialog") return;
            regionMap = null;
            regionLayers = null;
        });
        window.ulHtmxActions?.register("saved-filter-open", openSavedFilterDialog);
    }
    if (inline) initFields();
}
