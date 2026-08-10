/**
 * The photo map inside an album's view: where each photo was taken, with
 * drag-to-correct on the viewer's own photos.
 *
 * Everything visual comes from the shared components - `createPhotoMarkerLayer`
 * for the markers and their drag behaviour, `createMapLayers` for the tile
 * sources and the layers strip - so this module is only the album-specific
 * wiring: read the photo list the server rendered, fit the view to it, and post
 * a moved photo to the gallery's own reposition endpoint.
 *
 * The album panel is HTMX-swapped, so the map is torn down and rebuilt per swap
 * rather than initialised once. Leaflet cannot measure a container inside a
 * `hidden` ancestor, so the map is only built once the section is shown, and
 * `invalidateSize` runs after it becomes visible.
 */

declare const L: typeof import("leaflet");

import { getCsrfToken } from "./csrf";
import { toast } from "./dialogs";
import { createMapLayers } from "./map-layers";
import { createPhotoMarkerLayer, type PhotoMapItem, type PhotoMarkerLayer } from "./photo-map";

/** Zoom used when an album has exactly one placed photo (fitBounds would max out). */
const SINGLE_PHOTO_ZOOM = 17;
/** Fallback zoom when no photo has a position and we centre on the place itself. */
const FALLBACK_ZOOM = 15;

interface AlbumMapHandle {
    map: L.Map;
    markers: PhotoMarkerLayer;
}

let current: AlbumMapHandle | null = null;

function panel(): HTMLElement | null {
    return document.getElementById("albums-panel");
}

/** The photo list the server rendered into the panel, or [] when absent/invalid. */
function readPhotos(): PhotoMapItem[] {
    const el = document.getElementById("album-map-photos");
    if (!el?.textContent) return [];
    try {
        const parsed = JSON.parse(el.textContent) as PhotoMapItem[];
        return Array.isArray(parsed) ? parsed : [];
    } catch {
        return [];
    }
}

/**
 * Persist a photo's new position.
 *
 * Posts to the pin/wiki gallery's per-image endpoint (`<gallery>/<id>/`) - the
 * single writer for Image coordinates, which also enforces that only the
 * uploader may move their own photo. Rejects so the marker snaps back.
 */
async function savePosition(imageId: number, lat: number, lng: number): Promise<void> {
    const base = panel()?.dataset.repositionBase;
    if (!base) throw new Error("No reposition endpoint for this album.");

    const response = await fetch(`${base}${imageId}/`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: JSON.stringify({ latitude: lat, longitude: lng }),
    });
    if (!response.ok) {
        const data = (await response.json().catch(() => ({}))) as { error?: string };
        throw new Error(data.error || `HTTP ${response.status}`);
    }
}

/** Tear down any existing album map, so a panel swap can't leave one orphaned. */
export function destroyAlbumMap(): void {
    if (!current) return;
    current.markers.destroy();
    current.map.remove();
    current = null;
}

/**
 * Build the album map, or re-measure it if it already exists.
 *
 * Safe to call repeatedly - the second call only invalidates the size, which is
 * what showing a previously-hidden section needs.
 */
export function initAlbumMap(): void {
    const container = document.getElementById("album-map");
    if (!container || typeof L === "undefined") return;

    if (current) {
        current.map.invalidateSize();
        return;
    }

    const photos = readPhotos();
    const centerLat = Number.parseFloat(container.dataset.centerLat ?? "");
    const centerLng = Number.parseFloat(container.dataset.centerLng ?? "");
    const fallback: [number, number] = [
        Number.isFinite(centerLat) ? centerLat : (photos[0]?.lat ?? 0),
        Number.isFinite(centerLng) ? centerLng : (photos[0]?.lng ?? 0),
    ];

    const map = L.map(container, { scrollWheelZoom: false, attributionControl: false }).setView(fallback, FALLBACK_ZOOM);

    createMapLayers(map, {
        root: document.getElementById("album-map-layers"),
        defaultBase: "remember",
        storageKey: "ul-album-map-layers",
    });

    const markers = createPhotoMarkerLayer(map, {
        onMove: (id, lat, lng) =>
            savePosition(id, lat, lng)
                .then(() => {
                    toast.success("Photo position saved.");
                })
                .catch((err: Error) => {
                    toast.error(`Could not move that photo: ${err.message}`);
                    throw err;
                }),
        onHover: (id, on) => {
            document.querySelectorAll<HTMLElement>(`.album-item[data-id="${id}"]`).forEach((el) => {
                el.classList.toggle("is-highlighted", on);
            });
        },
    });
    markers.replaceAll(photos);

    const bounds = markers.bounds();
    if (bounds && markers.count() > 1) {
        map.fitBounds(bounds, { padding: [40, 40] });
    } else if (bounds) {
        map.setView(bounds.getCenter(), SINGLE_PHOTO_ZOOM);
    }

    current = { map, markers };
    // The section was hidden until the moment this ran; Leaflet measured a
    // zero-height container, so re-measure once the browser has laid it out.
    window.setTimeout(() => map.invalidateSize(), 0);
}

/** Highlight an album grid tile's marker, for grid-to-map hover sync. */
export function highlightAlbumPhoto(imageId: number, on: boolean): void {
    current?.markers.highlight(imageId, on);
}

/** Whether a map is currently built (so callers can skip needless work). */
export function albumMapIsOpen(): boolean {
    return current !== null;
}
