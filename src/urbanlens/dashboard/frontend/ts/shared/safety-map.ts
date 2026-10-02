/**
 * The check-in map (``partials/safety/_safety_map.html``): the destination pin, the shared layers panel and search
 * bar, and the check-in's markup - editable for its owner, read-only for contacts and the community.
 */

import { getCsrfToken } from "./csrf";
import type { ShapeSpec } from "./markup-engine";
import { markupItemToShapeSpec } from "./markup-shape";
import type { MarkupItem, MarkupToolbar } from "./markup-toolbar";
import type { FetchInit } from "./site-runtime";

declare const L: typeof import("leaflet") | undefined;
type Leaflet = typeof import("leaflet");

const MARKUP_MAP_UUID_PLACEHOLDER = "11111111-1111-1111-1111-111111111111";
const LEGACY_SEARCH_HISTORY_KEY = "ul_safety_dest_history_v1";

function isMarkupItem(value: unknown): value is MarkupItem {
    return !!value && typeof value === "object" && "markup_type" in value && typeof value.markup_type === "string" && "geometry" in value && !!value.geometry && typeof value.geometry === "object";
}

function markupItemsOf(payload: unknown): unknown[] {
    return payload && typeof payload === "object" && "markup_items" in payload && Array.isArray(payload.markup_items) ? payload.markup_items : [];
}

/** The shapes a read-only viewer sees, converted exactly as the owner's editor converts them. */
export function readonlyMarkupShapes(payload: unknown): ShapeSpec[] {
    const shapes: ShapeSpec[] = [];
    for (const item of markupItemsOf(payload)) {
        const shape = isMarkupItem(item) ? markupItemToShapeSpec(item) : null;
        if (shape) shapes.push(shape);
    }
    return shapes;
}

/**
 * Destination searches are the places someone was about to explore, so their history is kept per profile. The unscoped
 * key once shared it with whoever next used the browser; it is dropped on sight.
 */
export function destinationSearchHistoryKey(profileUuid: string): string {
    try {
        localStorage.removeItem(LEGACY_SEARCH_HISTORY_KEY);
    } catch {
        // Storage unavailable.
    }
    return `${LEGACY_SEARCH_HISTORY_KEY}_${profileUuid}`;
}

function number(value: string | undefined, fallback: number): number {
    const parsed = Number.parseFloat(value ?? "");
    return Number.isFinite(parsed) ? parsed : fallback;
}

async function loadReadonlyMarkup(leaflet: Leaflet, map: L.Map, url: string, wrapper: HTMLElement): Promise<void> {
    const layer = leaflet.layerGroup().addTo(map);
    try {
        const payload: unknown = await (await fetch(url)).json();
        for (const shape of readonlyMarkupShapes(payload)) window.MarkupEngine.renderShape(shape, layer, map.getZoom());
        const truncated = payload && typeof payload === "object" && "truncated" in payload ? payload.truncated : undefined;
        window.MarkupEngine.reportMarkupTruncation(wrapper, { markup_items: markupItemsOf(payload), truncated });
    } catch {
        // The map is still useful without its markup.
    }
}

export function installSafetyMap(wrapper: HTMLElement): void {
    const el = wrapper.querySelector<HTMLElement>("#safety-map");
    const leaflet = typeof L === "undefined" ? undefined : L;
    if (!el || !leaflet) return;
    const d = wrapper.dataset;
    const readonly = d.readonly === "true";

    const map = leaflet.map(el, { attributionControl: false }).setView([number(d.centerLat, 39.8283), number(d.centerLng, -98.5795)], number(d.zoom, 4));
    // The toolbar's screenshot tool and the live-location marker read it.
    window.map = map;
    setTimeout(() => map.invalidateSize(), 300);
    // The markup toolbar draws into this pane; only the pin page's own entry would create it otherwise.
    const pane = map.createPane("markupPane");
    pane.style.zIndex = "550";

    let layerMode = d.layerMode || "street";
    let showBorders = d.showBorders === "true";
    const viewUrlFor = (uuid: string | undefined): string | null => (uuid && d.mapViewUrlTemplate ? d.mapViewUrlTemplate.replaceAll(MARKUP_MAP_UUID_PLACEHOLDER, uuid) : null);
    let viewUrl = viewUrlFor(d.markupMapUuid);
    let saveTimer: ReturnType<typeof setTimeout> | undefined;
    // Keep the backing MarkupMap's viewport in step, so it reopens as it was left.
    const saveViewSoon = (): void => {
        const url = viewUrl;
        if (!url) return;
        clearTimeout(saveTimer);
        saveTimer = setTimeout(() => {
            const center = map.getCenter();
            const init: FetchInit = {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
                body: JSON.stringify({ center_lat: center.lat, center_lng: center.lng, zoom: map.getZoom(), layer_mode: layerMode, show_borders: showBorders }),
                __ulReported: true,
            };
            fetch(url, init).catch(() => {});
        }, 800);
    };
    map.on("moveend zoomend", saveViewSoon);
    if (d.markupMapCreateUrl) {
        document.addEventListener("ul:markup-map-created", (event) => {
            const uuid = event instanceof CustomEvent && event.detail && typeof event.detail.uuid === "string" ? event.detail.uuid : undefined;
            viewUrl = viewUrlFor(uuid) ?? viewUrl;
            saveViewSoon();
        });
    }

    window.MapLayers.create(map, {
        root: document.getElementById("safety-map-layers"),
        defaultBase: layerMode,
        initialOverlays: showBorders ? ["borders"] : [],
        onStateChange: (state) => {
            layerMode = state.base;
            showBorders = state.borders;
            saveViewSoon();
        },
    });

    let toolbar: MarkupToolbar | null = null;
    if (!readonly && (d.markupJsonUrl || d.markupMapCreateUrl)) {
        toolbar = window.createMarkupToolbar(map, leaflet.layerGroup().addTo(map), {
            markupJsonUrl: d.markupJsonUrl ?? "",
            markupCreateUrl: d.markupCreateUrl ?? "",
            markupEditUrlTemplate: d.markupEditUrlTemplate ?? "",
            markupMapCreateUrl: d.markupMapCreateUrl ?? "",
            markupMapUuid: d.markupMapUuid || null,
            markupMapJsonUrlTemplate: d.markupMapJsonUrlTemplate ?? "",
            markupMapMarkupUrlTemplate: d.markupMapMarkupUrlTemplate ?? "",
            markupMapMarkupEditUrlTemplate: d.markupMapMarkupEditUrlTemplate ?? "",
            markupMapFieldId: d.markupMapFieldId || "markup-map-uuid-field",
            markupFillOpacity: number(d.markupFillOpacity, 87),
            markupBorderOpacity: number(d.markupBorderOpacity, 100),
            lineFinishTipDismissed: () => d.lineTipDismissed === "true",
            getInitialView: () => {
                const center = map.getCenter();
                return { center_lat: center.lat, center_lng: center.lng, zoom: map.getZoom(), layer_mode: layerMode, show_borders: showBorders };
            },
        });
    } else if (readonly && d.markupJsonUrl) {
        void loadReadonlyMarkup(leaflet, map, d.markupJsonUrl, wrapper);
    }

    let marker: L.Marker | null = null;
    const placeMarker = (lat: number, lng: number): L.Marker => {
        const placed = leaflet.marker([lat, lng], { draggable: !readonly }).addTo(map);
        if (!readonly) {
            let dragStart: L.LatLng | null = null;
            placed.on("dragstart", () => {
                dragStart = placed.getLatLng();
            });
            // Moving the destination of a safety check-in is confirmed; a decline snaps the pin back.
            placed.on("dragend", async () => {
                const dropped = placed.getLatLng();
                const priorLatLng = dragStart;
                const message = "Set the destination to this location?";
                const ok = window.confirmDialog ? (await window.confirmDialog({ title: "Move destination?", message, confirmLabel: "Move pin", danger: false })) === true : window.confirm(message);
                if (ok) setDestination(dropped.lat, dropped.lng);
                else if (priorLatLng) placed.setLatLng(priorLatLng);
            });
        }
        return placed;
    };
    const setDestination = (lat: number, lng: number): void => {
        const latInput = document.getElementById("safety-destination-lat");
        const lngInput = document.getElementById("safety-destination-lng");
        if (latInput instanceof HTMLInputElement) latInput.value = lat.toFixed(6);
        if (lngInput instanceof HTMLInputElement) lngInput.value = lng.toFixed(6);
        // The check-in form's autosave hears this.
        latInput?.dispatchEvent(new Event("change", { bubbles: true }));
        if (marker) marker.setLatLng([lat, lng]);
        else marker = placeMarker(lat, lng);
    };

    const destLat = number(d.destLat, Number.NaN);
    const destLng = number(d.destLng, Number.NaN);
    if (Number.isFinite(destLat) && Number.isFinite(destLng)) marker = placeMarker(destLat, destLng);
    if (readonly) return;

    // A click places the first pin only; after that only a confirmed drag moves it. A click that is part of drawing
    // markup is not a destination.
    map.on("click", (event) => {
        if (marker || toolbar?.isDrawBusy()) return;
        setDestination(event.latlng.lat, event.latlng.lng);
    });

    window.LocationSearchEngine.attach("safety-map", {
        historyKey: destinationSearchHistoryKey(d.profileUuid ?? ""),
        sources: {
            localPins: { url: d.localPinsUrl ?? "" },
            googlePlaces: { url: d.placesUrl ?? "" },
        },
        resolvePlaceUrl: d.resolvePlaceUrl ?? null,
        pinCacheProfileUuid: d.profileUuid ?? "",
        enableMyLocation: true,
        defaultZoom: 15,
        onSelect: (result) => {
            map.setView([result.lat, result.lng], result.zoom || 15);
            setDestination(result.lat, result.lng);
        },
    });
}
