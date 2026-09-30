/**
 * A pin list's page (pages/pin_lists/detail.html): item reordering, the list's own actions, the smart-list
 * panel and its boundary map, the Add Pins search, and the overview map of the list's pins.
 */

import { byId } from "../shared/dom";
import Sortable from "sortablejs";
import type {} from "leaflet-draw";

import { getCsrfToken } from "../shared/csrf";
import { confirmAction, toast } from "../shared/dialogs";
import { startEditInPlace, type EditInPlaceOptions } from "../shared/edit-in-place";
import { overviewIcon, overviewPopupHtml, type OverviewPoint } from "../shared/pin-list-overview";
import { installSavedFilterForm } from "../shared/saved-filter-form";

declare const L: typeof import("leaflet");

const US_CENTER: [number, number] = [39.8283, -98.5795];

interface PinListConfig {
    editUrl: string;
    deleteUrl: string;
    deletedUrl: string;
    itemsUrl: string;
    itemsAddUrl: string;
    createTripUrl: string;
    addToTripUrl: string;
    markupMapUrl: string;
    pinAddUrl: string;
    localUrl: string;
    placesUrl: string;
    resolveUrl: string;
    profileUuid: string;
    center: [number, number];
    boundary: GeoJSON.Geometry | null;
}

interface RedirectResponse {
    ok?: boolean;
    redirect?: string;
    error?: string;
}

type InlineEditOptions = Omit<EditInPlaceOptions, "save">;

function readConfig(root: HTMLElement): PinListConfig {
    const d = root.dataset;
    const lat = Number.parseFloat(d.centerLat ?? "");
    const lng = Number.parseFloat(d.centerLng ?? "");
    let boundary: GeoJSON.Geometry | null = null;
    if (d.boundary) {
        try {
            boundary = JSON.parse(d.boundary) as GeoJSON.Geometry;
        } catch {
            boundary = null;
        }
    }
    return {
        editUrl: d.editUrl ?? "",
        deleteUrl: d.deleteUrl ?? "",
        deletedUrl: d.deletedUrl ?? "",
        itemsUrl: d.itemsUrl ?? "",
        itemsAddUrl: d.itemsAddUrl ?? "",
        createTripUrl: d.createTripUrl ?? "",
        addToTripUrl: d.addToTripUrl ?? "",
        markupMapUrl: d.markupMapUrl ?? "",
        pinAddUrl: d.pinAddUrl ?? "",
        localUrl: d.localUrl ?? "",
        placesUrl: d.placesUrl ?? "",
        resolveUrl: d.resolveUrl ?? "",
        profileUuid: d.profileUuid ?? "",
        center: Number.isNaN(lat) || Number.isNaN(lng) ? US_CENTER : [lat, lng],
        boundary,
    };
}

function toastError(message: unknown): void {
    toast.error(String(message || "Something went wrong."));
}

/**
 * POST JSON, resolving to the parsed body and rejecting with the server's message, whether it sent JSON or text.
 */
async function postJson(url: string, payload: unknown): Promise<RedirectResponse> {
    const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: JSON.stringify(payload),
    });
    const text = await response.text();
    let data: RedirectResponse = {};
    try {
        data = text ? (JSON.parse(text) as RedirectResponse) : {};
    } catch {
        data = {};
    }
    if (!response.ok) throw data.error || text || "Something went wrong.";
    return data;
}

function followRedirect(data: RedirectResponse): void {
    if (data.ok && data.redirect) window.location.href = data.redirect;
}

class PinListPage {
    private sortable: Sortable | null = null;
    private boundaryMap: L.Map | null = null;
    private boundaryLayers: L.FeatureGroup | null = null;
    private overviewMap: L.Map | null = null;
    private overviewMarkers: L.LayerGroup | null = null;
    private pendingCreatePin: { title: string; lat: number; lng: number } | null = null;
    private addPinsSearch: { clear: () => void } | null = null;

    constructor(private readonly cfg: PinListConfig) {}

    bind(): void {
        this.initSortable();
        this.bindInlineEditing();
        this.bindActions();
        this.bindSmartControls();
        this.bindAddPins();
        if (this.cfg.boundary) this.initBoundaryMap();
        this.syncOverviewMap();

        document.body.addEventListener("htmx:afterSwap", (e) => {
            const target = (e as CustomEvent<{ target?: Element }>).detail?.target;
            if (target?.id === "pin-list-items") this.itemsChanged();
        });
    }

    private itemsChanged(): void {
        this.initSortable();
        this.syncOverviewMap();
    }

    private replaceItems(html: string): void {
        const current = byId("pin-list-items", HTMLElement);
        if (current) current.outerHTML = html;
        this.itemsChanged();
    }

    private refreshItems(): void {
        fetch(this.cfg.itemsUrl)
            .then((r) => (r.ok ? r.text() : Promise.reject("Could not refresh this list.")))
            .then((html) => this.replaceItems(html))
            .catch(toastError);
    }

    // -- Reordering ------------------------------------------------------------

    private initSortable(): void {
        const list = byId("pin-list-items", HTMLElement);
        this.sortable?.destroy();
        this.sortable = null;
        if (!list) return;
        this.sortable = new Sortable(list, {
            animation: 150,
            handle: ".pin-list-item-drag-handle",
            ghostClass: "pin-list-item--ghost",
            onEnd: () => this.saveOrder(list),
        });
    }

    private saveOrder(list: HTMLElement): void {
        const items = Array.from(list.querySelectorAll<HTMLElement>(".pin-list-item[data-id]")).map((el) => ({ id: el.dataset.id }));
        // The DOM already shows the new order, so a silent failure reads as saved until the next load undoes it.
        postJson(list.dataset.saveUrl ?? "", { items }).catch(() => toastError("Could not save the new order."));
    }

    // -- Title and description, edited in place --------------------------------

    private bindInlineEditing(): void {
        this.wireInlineEditable(document.querySelector(".pin-list-title-editable"), "name", {
            dataKey: "rawName",
            inputClass: "pin-list-title-input",
            maxLength: 100,
            successMessage: "List renamed.",
        });
        this.wireInlineEditable(document.querySelector(".pin-list-description-editable"), "description", {
            dataKey: "rawDescription",
            inputClass: "pin-list-description-input",
            multiline: true,
            allowEmpty: true,
            placeholder: "Add a description...",
            maxLength: 50000,
            successMessage: "Description updated.",
        });
    }

    private wireInlineEditable(el: HTMLElement | null, field: string, opts: InlineEditOptions): void {
        if (!el) return;
        const start = (): void => startEditInPlace(el, { ...opts, save: (value) => postJson(this.cfg.editUrl, { [field]: value }) });
        el.addEventListener("click", start);
        el.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                start();
            }
        });
    }

    // -- Buttons -----------------------------------------------------------------

    private bindActions(): void {
        document.addEventListener("click", (e) => {
            const target = e.target instanceof Element ? e.target : null;
            const menu = document.querySelector(".pin-list-more-menu");
            if (menu && target && (!menu.contains(target) || target.closest("#pin-list-more-menu-panel button"))) this.closeMoreMenu();

            const control = target?.closest<HTMLElement>("[data-pl-action]");
            if (control) this.runAction(control);
        });
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape") this.closeMoreMenu();
        });
        byId("edit-list-form", HTMLFormElement)?.addEventListener("submit", (e) => {
            e.preventDefault();
            const name = byId("edit-list-name", HTMLInputElement)?.value.trim() ?? "";
            const description = byId("edit-list-description", HTMLTextAreaElement)?.value ?? "";
            postJson(this.cfg.editUrl, { name, description })
                .then(() => window.location.reload())
                .catch(toastError);
        });
    }

    private runAction(control: HTMLElement): void {
        switch (control.dataset.plAction) {
            case "toggle-more-menu":
                this.toggleMoreMenu();
                break;
            case "show-smart-panel":
                this.showSmartPanel();
                break;
            case "toggle-smart-panel":
                this.toggleSmartPanel();
                break;
            case "boundary-draw":
                this.startBoundaryDraw();
                break;
            case "boundary-clear":
                this.clearBoundary();
                break;
            case "create-trip":
                postJson(this.cfg.createTripUrl, {}).then(followRedirect).catch(toastError);
                break;
            case "create-markup-map":
                postJson(this.cfg.markupMapUrl, {}).then(followRedirect).catch(toastError);
                break;
            case "add-to-trip":
                postJson(this.cfg.addToTripUrl, { trip_slug: control.dataset.tripSlug ?? "" }).then(followRedirect).catch(toastError);
                break;
            case "export":
                this.exportList(control.dataset.format ?? "");
                break;
            case "delete":
                void this.deleteList();
                break;
        }
    }

    private toggleMoreMenu(): void {
        const panel = byId("pin-list-more-menu-panel", HTMLElement);
        if (!panel) return;
        const opening = panel.hidden;
        panel.hidden = !opening;
        byId("pin-list-more-btn", HTMLElement)?.setAttribute("aria-expanded", String(opening));
    }

    private closeMoreMenu(): void {
        const panel = byId("pin-list-more-menu-panel", HTMLElement);
        if (!panel || panel.hidden) return;
        panel.hidden = true;
        byId("pin-list-more-btn", HTMLElement)?.setAttribute("aria-expanded", "false");
    }

    private async deleteList(): Promise<void> {
        const confirmed = await confirmAction({
            title: "Delete this list?",
            message: "The pins on it are untouched, and you can restore the list from Settings → Undo History.",
            confirmLabel: "Delete list",
        });
        if (!confirmed) return;
        fetch(this.cfg.deleteUrl, { method: "POST", headers: { "X-CSRFToken": getCsrfToken() } })
            .then((r) => {
                if (!r.ok) throw "Could not delete this list.";
                window.location.href = this.cfg.deletedUrl;
            })
            .catch(toastError);
    }

    /** A plain form POST, so the browser downloads the response through its own Content-Disposition handling. */
    private exportList(format: string): void {
        const field = byId("export-list-format-field", HTMLInputElement);
        if (field) field.value = format;
        byId("export-list-form", HTMLFormElement)?.submit();
        byId("export-list-dialog", HTMLDialogElement)?.close();
    }

    // -- Smart list panel ----------------------------------------------------------

    private setSmartPanelOpen(open: boolean): void {
        const body = byId("pin-list-smart-body", HTMLElement);
        if (!body) return;
        body.hidden = !open;
        byId("pin-list-smart-toggle-btn", HTMLElement)?.setAttribute("aria-expanded", String(open));
        const chevron = byId("pin-list-smart-chevron", HTMLElement);
        if (chevron) chevron.textContent = open ? "expand_more" : "chevron_right";
        // A boundary map built while the panel was collapsed measured a 0x0 container.
        if (open) this.resizeBoundaryMap();
    }

    private toggleSmartPanel(): void {
        this.setSmartPanelOpen(byId("pin-list-smart-body", HTMLElement)?.hidden === true);
    }

    /** Reveals the panel on a plain list, where it is hidden entirely, and retires the button that asked for it. */
    private showSmartPanel(): void {
        const section = byId("pin-list-smart-panel", HTMLElement);
        if (section) section.hidden = false;
        this.setSmartPanelOpen(true);
        byId("pin-list-add-smart-filter-btn", HTMLElement)?.setAttribute("hidden", "hidden");
        section?.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    private revealSmartEnableRow(): void {
        const row = byId("pin-list-smart-enable-row", HTMLElement);
        if (row) row.hidden = false;
    }

    private bindSmartControls(): void {
        const savedFilter = byId("pin-list-saved-filter-select", HTMLSelectElement);
        savedFilter?.addEventListener("change", () => {
            const uuid = savedFilter.value;
            if (uuid) this.revealSmartEnableRow();
            postJson(this.cfg.editUrl, { saved_filter_uuid: uuid })
                .then(() => this.refreshItems())
                .catch(toastError);
        });
        const isSmart = byId("pin-list-is-smart", HTMLInputElement);
        isSmart?.addEventListener("change", () => {
            const checked = isSmart.checked;
            postJson(this.cfg.editUrl, { is_smart: checked })
                .then(() => this.refreshItems())
                .catch((err) => {
                    toastError(err);
                    isSmart.checked = !checked;
                });
        });
    }

    // -- Boundary map ----------------------------------------------------------------

    private setBoundaryDrawingState(hasBoundary: boolean): void {
        const draw = byId("pin-list-boundary-draw-btn", HTMLElement);
        const clear = byId("pin-list-boundary-clear-btn", HTMLElement);
        const map = byId("pin-list-boundary-map", HTMLElement);
        if (draw) draw.hidden = hasBoundary;
        if (clear) clear.hidden = !hasBoundary;
        if (map) map.hidden = !hasBoundary;
    }

    private resizeBoundaryMap(): void {
        const map = this.boundaryMap;
        if (map) requestAnimationFrame(() => map.invalidateSize());
    }

    private startBoundaryDraw(): void {
        this.setBoundaryDrawingState(true);
        this.initBoundaryMap();
        this.resizeBoundaryMap();
    }

    private clearBoundary(): void {
        this.boundaryLayers?.clearLayers();
        postJson(this.cfg.editUrl, { smart_boundary: null })
            .then(() => {
                this.refreshItems();
                this.setBoundaryDrawingState(false);
            })
            .catch(toastError);
    }

    /** Built eagerly for a stored boundary, otherwise once the draw button has made its container visible. */
    private initBoundaryMap(): void {
        if (this.boundaryMap) return;
        const mapEl = byId("pin-list-boundary-map", HTMLElement);
        if (!mapEl) return;
        const map = L.map(mapEl, { attributionControl: false }).setView(this.cfg.center, 4);
        this.boundaryMap = map;
        window.MapLayers.tileLayer("street").addTo(map);
        const drawn = new L.FeatureGroup().addTo(map);
        this.boundaryLayers = drawn;

        if (this.cfg.boundary) {
            try {
                window.RegionDelete.polygonParts(this.cfg.boundary).forEach((part) => L.geoJSON(part).eachLayer((layer) => drawn.addLayer(layer)));
                if (drawn.getLayers().length) map.fitBounds(drawn.getBounds());
            } catch {
                // Malformed stored geometry leaves the map empty rather than breaking the page.
            }
        }

        map.addControl(
            new L.Control.Draw({
                draw: { polygon: {}, marker: false, circle: false, circlemarker: false, rectangle: false, polyline: false },
                edit: { featureGroup: drawn, remove: false },
            }),
        );
        const save = (): void => {
            const layers = drawn.getLayers() as L.Polygon[];
            const geometry = layers.length === 0 ? null : { type: "MultiPolygon", coordinates: layers.map((l) => l.toGeoJSON().geometry.coordinates) };
            this.setBoundaryDrawingState(layers.length > 0);
            if (layers.length > 0) this.revealSmartEnableRow();
            postJson(this.cfg.editUrl, { smart_boundary: geometry })
                .then(() => this.refreshItems())
                .catch(toastError);
        };
        window.RegionDelete.add(map, drawn, save);
        map.on(L.Draw.Event.CREATED, (e) => {
            if ("layer" in e && e.layer instanceof L.Layer) drawn.addLayer(e.layer);
            save();
        });
        map.on(L.Draw.Event.EDITED, save);
        map.on(L.Draw.Event.DELETED, save);
    }

    // -- Add Pins ----------------------------------------------------------------------

    private bindAddPins(): void {
        this.addPinsSearch = window.LocationSearchEngine.attach("add-pins", {
            sources: {
                localPins: { url: this.cfg.localUrl },
                googlePlaces: { url: this.cfg.placesUrl },
            },
            resolvePlaceUrl: this.cfg.resolveUrl,
            pinCacheProfileUuid: this.cfg.profileUuid,
            enableMyLocation: false,
            defaultZoom: 15,
            onSelect: (result) => {
                if (result.pinSlug) {
                    this.addPinBySlug(result.pinSlug);
                    this.addPinsSearch?.clear();
                    return;
                }
                this.pendingCreatePin = { title: result.title, lat: result.lat, lng: result.lng };
                const text = byId("add-pins-create-confirm-text", HTMLElement);
                if (text) text.textContent = `Create a new pin for "${result.title}" and add it to this list?`;
                const name = byId("add-pins-create-confirm-name", HTMLInputElement);
                if (name) name.value = result.title || "";
                byId("add-pins-create-confirm-dialog", HTMLDialogElement)?.showModal();
            },
        });

        const confirmBtn = byId("add-pins-create-confirm-btn", HTMLButtonElement);
        confirmBtn?.addEventListener("click", () => {
            const pending = this.pendingCreatePin;
            if (!pending) return;
            const body = new FormData();
            body.append("name", byId("add-pins-create-confirm-name", HTMLInputElement)?.value.trim() || pending.title || "New Pin");
            body.append("latitude", String(pending.lat));
            body.append("longitude", String(pending.lng));
            confirmBtn.disabled = true;
            fetch(this.cfg.pinAddUrl, { method: "POST", headers: { "X-CSRFToken": getCsrfToken() }, body })
                .then((r) => (r.ok ? (r.json() as Promise<{ pin_slug?: string }>) : r.text().then((t) => Promise.reject(t || "Failed to create pin."))))
                .then((data) => {
                    byId("add-pins-create-confirm-dialog", HTMLDialogElement)?.close();
                    this.pendingCreatePin = null;
                    if (data.pin_slug) this.addPinBySlug(data.pin_slug);
                    this.addPinsSearch?.clear();
                })
                .catch(toastError)
                .finally(() => {
                    confirmBtn.disabled = false;
                });
        });
    }

    private addPinBySlug(slug: string): void {
        fetch(this.cfg.itemsAddUrl, { method: "POST", headers: { "X-CSRFToken": getCsrfToken() }, body: new URLSearchParams({ pin_slugs: slug }) })
            .then((r) => (r.ok ? r.text() : Promise.reject("Could not add that pin.")))
            .then((html) => {
                this.replaceItems(html);
                toast.success("Added to list.");
            })
            .catch(toastError);
    }

    // -- Overview map ------------------------------------------------------------------

    /** Reads the JSON the items panel embeds, so it follows every swap of that panel. */
    private syncOverviewMap(): void {
        const mapEl = byId("pin-list-map", HTMLElement);
        if (!mapEl) return;
        let points: OverviewPoint[] = [];
        try {
            points = JSON.parse(byId("pin-list-items-map-data", HTMLElement)?.textContent || "[]") as OverviewPoint[];
        } catch {
            points = [];
        }

        if (!this.overviewMap) {
            // A view from the start, so the screenshot tool's getCenter() works on an empty list too.
            this.overviewMap = L.map(mapEl, { attributionControl: false }).setView(US_CENTER, 4);
            window.map = this.overviewMap;
            window.MapLayers.create(this.overviewMap, {
                root: byId("pin-list-map-layers", HTMLElement),
                onAttribution: window.MapLayers.setAttribution,
            });
            this.overviewMarkers = L.layerGroup().addTo(this.overviewMap);
        }
        const map = this.overviewMap;
        const section = byId("pin-list-map-section", HTMLElement);
        if (section) section.hidden = points.length === 0;
        if (!points.length) return;
        this.overviewMarkers?.clearLayers();

        const markers = points.map((pt) => {
            const marker = L.marker([pt.latitude, pt.longitude]);
            const icon = overviewIcon(pt);
            if (icon) {
                const size = icon.circled ? 36 : 28;
                marker.setIcon(L.divIcon({ className: "map-pin-icon-wrap", html: icon.html, iconSize: [size, size], iconAnchor: [size / 2, size / 2] }));
            }
            marker.bindPopup(overviewPopupHtml(pt));
            if (this.overviewMarkers) marker.addTo(this.overviewMarkers);
            return marker;
        });

        const [only] = points;
        if (markers.length > 1) map.fitBounds(L.featureGroup(markers).getBounds().pad(0.15));
        else if (only) map.setView([only.latitude, only.longitude], 14);
        setTimeout(() => map.invalidateSize(), 0);
    }
}

const root = document.querySelector<HTMLElement>(".pin-list-detail-page");
if (root) new PinListPage(readConfig(root)).bind();
// "Create a saved filter from this list" opens the shared saved-filter dialog.
installSavedFilterForm();
