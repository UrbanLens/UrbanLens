/**
 * A saved filter's own page (``pages/pin_lists/saved_filter_detail.html``): the map of the pins it matches,
 * redrawn as the form's criteria change. Values arrive on ``#saved-filter-preview-map``.
 */

import { getCsrfToken } from "./csrf";
import { fetchJson } from "./fetch-json";

declare const L: typeof import("leaflet");

export interface PreviewPin {
    lat: number;
    lng: number;
    slug: string;
    name?: string;
}

interface PreviewResponse {
    pins?: PreviewPin[];
    count?: number;
}

const PREVIEW_DELAY_MS = 500;

/** "12 matching pins", saying so when the map shows only a sample of them. */
export function matchLabel(count: number, shown: number, limit: string): string {
    const label = `${count} matching pin${count === 1 ? "" : "s"}`;
    return shown < count ? `${label} (showing ${limit} on the map, spread across the area)` : label;
}

function popup(pin: PreviewPin): HTMLElement {
    const div = document.createElement("div");
    const link = document.createElement("a");
    link.href = `/dashboard/map/pin/${encodeURIComponent(pin.slug)}/`;
    link.textContent = pin.name || "View pin";
    div.appendChild(link);
    return div;
}

export class SavedFilterPreview {
    private map: L.Map | null = null;
    private markers: L.LayerGroup | null = null;
    private timer: number | undefined;
    private token = 0;

    constructor(private readonly mapEl: HTMLElement) {}

    install(): void {
        let initial: PreviewPin[] = [];
        try {
            initial = JSON.parse(document.getElementById("saved-filter-initial-pins")?.textContent ?? "[]");
        } catch {
            initial = [];
        }
        this.paint(initial);
        this.showCount(Number(this.mapEl.dataset.matchCount) || 0, initial.length);
        const form = document.getElementById("saved-filter-form");
        form?.addEventListener("change", () => this.schedule());
        form?.addEventListener("input", () => this.schedule());
    }

    private status(): HTMLElement | null {
        return document.getElementById("saved-filter-preview-status");
    }

    private showCount(count: number, shown: number): void {
        const el = document.getElementById("saved-filter-preview-count");
        if (el) el.textContent = matchLabel(count, shown, this.mapEl.dataset.pinLimit ?? "");
    }

    private paint(pins: PreviewPin[]): void {
        if (!this.map) {
            const center: [number, number] = [Number(this.mapEl.dataset.centerLat) || 39.8283, Number(this.mapEl.dataset.centerLng) || -98.5795];
            this.map = L.map(this.mapEl, { attributionControl: false }).setView(center, 4);
            // The map toolbar's screenshot button reads window.map.
            window.map = this.map;
            window.MapLayers.create(this.map, {
                root: document.getElementById("saved-filter-preview-map-layers"),
                onAttribution: window.MapLayers.setAttribution,
            });
            this.markers = L.layerGroup().addTo(this.map);
        }
        const map = this.map;
        const group = this.markers;
        if (!group) return;
        group.clearLayers();
        const markers = pins.map((pin) => L.marker([pin.lat, pin.lng]).bindPopup(popup(pin)).addTo(group));
        const first = pins[0];
        if (markers.length > 1) map.fitBounds(L.featureGroup(markers).getBounds().pad(0.15));
        else if (first) map.setView([first.lat, first.lng], 14);
        window.setTimeout(() => map.invalidateSize(), 0);
    }

    private schedule(): void {
        window.clearTimeout(this.timer);
        const status = this.status();
        if (status) status.hidden = false;
        this.timer = window.setTimeout(() => void this.run(), PREVIEW_DELAY_MS);
    }

    private async run(): Promise<void> {
        const form = document.getElementById("saved-filter-form");
        const url = this.mapEl.dataset.previewUrl;
        if (!(form instanceof HTMLFormElement) || !url) return;
        const token = ++this.token;
        let data: PreviewResponse | null = null;
        try {
            data = await fetchJson<PreviewResponse>(url, { method: "POST", headers: { "X-CSRFToken": getCsrfToken() }, body: new FormData(form), reportsItsOwnErrors: true });
        } catch {
            data = null;
        }
        // A newer request has superseded this one.
        if (token !== this.token) return;
        const status = this.status();
        if (status) status.hidden = true;
        if (!data) return;
        const pins = data.pins ?? [];
        this.paint(pins);
        this.showCount(data.count ?? 0, pins.length);
    }
}
