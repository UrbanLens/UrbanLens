/**
 * The "Create a pin" dialog on Vault > Photos: a draggable marker at the photo's location, the shared place search,
 * and the viewer's nearby pins, any of which can take the photo instead of a new pin.
 */

import type * as Leaflet from "leaflet";

declare const L: typeof import("leaflet");

const DIALOG_ID = "photo-pin-confirm-dialog";
const BODY_ID = "photo-pin-confirm-body";
const LOADING_HTML = '<p class="photo-pin-confirm-loading">Loading&hellip;</p>';

export interface NearbyPin {
    latitude?: number | null;
    longitude?: number | null;
    slug?: string;
    name?: string;
}

/** The `bbox` query value `map.pins` expects: south,west,north,east. */
export function bboxParam(bounds: Leaflet.LatLngBounds): string {
    return [bounds.getSouth(), bounds.getWest(), bounds.getNorth(), bounds.getEast()].join(",");
}

/** A nearby pin's popup: its name, and a button that files the photo onto it. */
export function nearbyPinPopup(name: string | undefined, onUse: () => void): HTMLElement {
    const popup = document.createElement("div");
    popup.className = "photo-pin-existing-popup";
    const title = document.createElement("strong");
    title.textContent = name || "Unnamed pin";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn--sm btn--primary photo-pin-use-existing";
    button.textContent = "Use this pin for this photo";
    button.addEventListener("click", onUse);
    popup.append(title, button);
    return popup;
}

function input(id: string): HTMLInputElement | null {
    const el = document.getElementById(id);
    return el instanceof HTMLInputElement ? el : null;
}

/** Owns the dialog's one Leaflet map, rebuilt each time the dialog body is reloaded for another photo. */
export class PhotoPinConfirm {
    private map: Leaflet.Map | null = null;
    private nearbyPins: Leaflet.LayerGroup | null = null;

    /** Load the dialog body for one photo from *url*, then open the dialog over it. */
    async load(url: string): Promise<void> {
        const body = document.getElementById(BODY_ID);
        const dialog = document.getElementById(DIALOG_ID);
        if (!body || !(dialog instanceof HTMLDialogElement) || !window.htmx) return;
        body.innerHTML = LOADING_HTML;
        await window.htmx.ajax("GET", url, { target: `#${BODY_ID}`, swap: "innerHTML" });
        dialog.showModal();
        this.init();
    }

    private init(): void {
        const root = document.getElementById("photo-pin-confirm");
        const mapEl = document.getElementById("photo-pin-confirm-map");
        if (!root || !mapEl || typeof L === "undefined") return;
        const lat = Number.parseFloat(root.dataset.lat ?? "");
        const lng = Number.parseFloat(root.dataset.lng ?? "");
        if (Number.isNaN(lat) || Number.isNaN(lng)) return;

        this.map?.remove();
        const map = L.map(mapEl, { attributionControl: false }).setView([lat, lng], 15);
        this.map = map;
        // Attribution renders below the map rather than as an on-map control.
        window.MapLayers.create(map, {
            root: document.getElementById("photo-pin-confirm-layers"),
            onAttribution: (text) => {
                const el = document.getElementById("photo-pin-confirm-attribution");
                if (el) el.textContent = text;
            },
        });
        const marker = L.marker([lat, lng], { draggable: true }).addTo(map);
        this.nearbyPins = L.layerGroup().addTo(map);

        marker.on("dragend", () => this.commit(marker.getLatLng()));
        map.on("click", (e: Leaflet.LeafletMouseEvent) => {
            marker.setLatLng(e.latlng);
            this.commit(e.latlng);
        });
        // Leaflet measured the dialog while it was still display:none.
        setTimeout(() => this.map?.invalidateSize(), 60);

        this.wireLocationSearch(root, marker);
        void this.loadNearbyPins(root);
        map.on("moveend", () => void this.loadNearbyPins(root));
    }

    private commit(latlng: Leaflet.LatLng): void {
        const latInput = input("photo-pin-confirm-lat");
        const lngInput = input("photo-pin-confirm-lng");
        if (latInput) latInput.value = latlng.lat.toFixed(6);
        if (lngInput) lngInput.value = latlng.lng.toFixed(6);
    }

    private wireLocationSearch(root: HTMLElement, marker: Leaflet.Marker): void {
        if (typeof window.LocationSearchEngine === "undefined") return;
        window.LocationSearchEngine.attach("photo-pin-place", {
            sources: {
                localPins: { url: root.dataset.localUrl ?? "" },
                osmNominatim: { url: root.dataset.nominatimUrl ?? "" },
                googlePlaces: { url: root.dataset.placesUrl ?? "" },
            },
            resolvePlaceUrl: root.dataset.resolveUrl,
            pinCacheProfileUuid: root.dataset.profileUuid,
            enableMyLocation: false,
            defaultZoom: 16,
            onSelect: (result) => {
                // One of the viewer's own pins takes the photo directly, rather than moving the new pin's marker.
                if (result.type === "pin" && result.pinSlug) {
                    void this.useExistingPin(root, result.pinSlug);
                    return;
                }
                const latlng = L.latLng(result.lat, result.lng);
                marker.setLatLng(latlng);
                this.map?.setView(latlng, result.zoom || 16);
                this.commit(latlng);
                const name = input("photo-pin-confirm-name");
                if (name && !name.value.trim()) name.value = result.title || "";
            },
        });
    }

    private async loadNearbyPins(root: HTMLElement): Promise<void> {
        const url = root.dataset.pinsUrl;
        if (!url || !this.map || !this.nearbyPins) return;
        try {
            const response = await fetch(`${url}?bbox=${encodeURIComponent(bboxParam(this.map.getBounds()))}`, { headers: { "X-Requested-With": "XMLHttpRequest" } });
            const data = (response.ok ? await response.json() : { pins: [] }) as { pins?: NearbyPin[] };
            this.renderNearbyPins(root, data.pins || []);
        } catch {
            // Best-effort: the search box still reaches every pin.
        }
    }

    private renderNearbyPins(root: HTMLElement, pins: NearbyPin[]): void {
        const layer = this.nearbyPins;
        if (!layer) return;
        layer.clearLayers();
        for (const pin of pins) {
            const slug = pin.slug;
            if (pin.latitude == null || pin.longitude == null || !slug) continue;
            L.circleMarker([pin.latitude, pin.longitude], { radius: 8, color: "#2563eb", weight: 2, fillColor: "#2563eb", fillOpacity: 0.75 })
                .bindPopup(nearbyPinPopup(pin.name, () => void this.useExistingPin(root, slug)))
                .addTo(layer);
        }
    }

    private async useExistingPin(root: HTMLElement, slug: string): Promise<void> {
        const url = root.dataset.logVisitUrl;
        if (!window.htmx || !url) return;
        await window.htmx.ajax("POST", url, { target: `#photo-card-${root.dataset.imageId}`, swap: "outerHTML", values: { pin_slug: slug } });
        const dialog = document.getElementById(DIALOG_ID);
        if (dialog instanceof HTMLDialogElement) dialog.close();
    }
}

/** Expose the dialog to the organize queue's cards, whose "Create pin" buttons call it inline. */
export function installPhotoPinConfirm(): PhotoPinConfirm {
    const confirm = new PhotoPinConfirm();
    window.photosLoadPinConfirm = (url) => void confirm.load(url);
    return confirm;
}
