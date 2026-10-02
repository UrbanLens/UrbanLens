/**
 * Placing a Media-section tile on a map. The tile is an external provider's result, not yet an Image row, so the
 * server saves a local copy at the drop point (`PinController.media_relevance`) before it can become a photo marker.
 */

declare const L: typeof import("leaflet");

import { toast } from "./dialogs";
import { sendJson } from "./fetch-json";
import { makePhotoIcon, photoMarkerSize } from "./photo-map";

/** A Media-section tile's payload, as carried by its "text/media-item" drag. */
export interface MediaDropItem {
    source: string;
    key: string;
    url: string;
    pageUrl: string;
    caption: string;
}

type MaterializedDrop = {
    image_id?: number;
    image_url?: string;
    latitude?: number | null;
    longitude?: number | null;
    materialize_error?: string;
};

/** The shared "still processing" pulse, on the tile and on a marker where it was dropped. Returns the undo. */
function showPending(map: L.Map, itemEl: HTMLElement | undefined, latlng: L.LatLng): () => void {
    itemEl?.classList.add("media-processing");
    itemEl?.setAttribute("aria-busy", "true");
    const size = photoMarkerSize(map.getZoom());
    // The thumbnail the tile already shows, never item.url: that is the provider's original, on the provider's host.
    const thumb = itemEl?.dataset.mediaThumb;
    const icon = thumb
        ? makePhotoIcon(thumb, size)
        : L.divIcon({ className: "", html: '<i class="material-symbols-outlined" aria-hidden="true">hourglass_top</i>', iconSize: [size, size] });
    const marker = L.marker(latlng, { icon, interactive: false, keyboard: false }).addTo(map);
    marker.getElement()?.classList.add("media-processing");
    return () => {
        itemEl?.classList.remove("media-processing");
        itemEl?.removeAttribute("aria-busy");
        marker.remove();
    };
}

/** Save *item* as a photo at *latlng*, showing it as pending on its tile and the map until the server answers. */
export async function placeMediaItem(map: L.Map, relevanceUrl: string, itemEl: HTMLElement | undefined, item: MediaDropItem, latlng: L.LatLng): Promise<void> {
    const clearPending = showPending(map, itemEl, latlng);
    try {
        const data = await sendJson<MaterializedDrop>(
            relevanceUrl,
            "POST",
            {
                source: item.source,
                item_key: item.key,
                url: item.url,
                is_relevant: true,
                page_url: item.pageUrl,
                caption: item.caption,
                latitude: latlng.lat,
                longitude: latlng.lng,
            },
            { reportsItsOwnErrors: true },
        );
        if (!data) return;
        // The gallery reports a failed local copy itself.
        if (window.mediaApplyMaterializedDrop) window.mediaApplyMaterializedDrop(itemEl, data);
        else if (data.materialize_error) toast.warning(`Couldn't save a local copy: ${data.materialize_error}`);
        if (data.image_id && data.image_url && data.latitude != null && data.longitude != null) {
            window._galleryAddMarker?.({ id: data.image_id, url: data.image_url, latitude: data.latitude, longitude: data.longitude });
        }
    } catch {
        toast.error("Failed to save photo location.");
    } finally {
        clearPending();
    }
}
