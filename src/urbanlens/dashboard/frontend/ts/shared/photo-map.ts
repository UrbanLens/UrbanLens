/**
 * Photo markers on a Leaflet map: the thumbnail icon, its zoom-dependent size,
 * and the drag-to-reposition behaviour.
 *
 * Extracted from the pin detail map (entries/map-annotations.ts), which remains
 * the canonical surface - it still owns its own marker bookkeeping because its
 * markers are also wired into a side panel, tap-to-place, and the lightbox. What
 * lives here is the part that must look and behave identically everywhere a
 * photo appears on a map, so a second surface (an album's map) can't drift into
 * a differently-sized icon or a differently-shaped save call.
 *
 * Leaflet is a CDN global on every map page, so it is declared rather than
 * imported - importing would bundle a second copy and clobber the plugins hung
 * off `window.L`.
 */

declare const L: typeof import("leaflet");

/** Base thumbnail size at zoom 16 and above. */
export const PHOTO_MARKER_BASE_SIZE = 44;
/** Floor for the zoomed-far-out case, below which a thumbnail is unreadable. */
export const PHOTO_MARKER_MIN_SIZE = 14;
/** How much a hovered/highlighted marker grows. */
export const PHOTO_MARKER_HOVER_SCALE = 56 / 44;

/**
 * Edge length for a photo marker at the given zoom.
 *
 * Full size at zoom 16+, halving every 4 zoom levels below that so thumbnails
 * don't cover a huge chunk of the map when zoomed far out; never smaller than
 * PHOTO_MARKER_MIN_SIZE.
 *
 * @param zoom - The map's current zoom level.
 * @param highlighted - Whether this marker is hovered/selected.
 */
export function photoMarkerSize(zoom: number, highlighted?: boolean): number {
    const scale = 2 ** ((zoom - 16) * 0.5);
    const base = Math.max(PHOTO_MARKER_MIN_SIZE, Math.min(PHOTO_MARKER_BASE_SIZE, Math.round(PHOTO_MARKER_BASE_SIZE * scale)));
    return highlighted ? Math.round(base * PHOTO_MARKER_HOVER_SCALE) : base;
}

/**
 * The square thumbnail icon used for a photo on any map.
 *
 * @param url - Image URL to show in the marker.
 * @param size - Edge length in pixels (see photoMarkerSize).
 * @param highlighted - Draws the selection ring when true.
 */
export function makePhotoIcon(url: string, size: number, highlighted?: boolean): L.DivIcon {
    const shadow = highlighted ? "0 0 0 3px #2563eb, 0 3px 10px rgba(0,0,0,.45)" : "0 2px 6px rgba(0,0,0,.35)";
    return L.divIcon({
        className: "",
        html: `<img src="${url}" class="photo-marker-img" style="width:${size}px;height:${size}px;object-fit:cover;border-radius:5px;border:2px solid #fff;box-shadow:${shadow};display:block;transition:transform .15s,box-shadow .15s;">`,
        iconSize: [size, size],
        iconAnchor: [size / 2, size / 2],
    });
}

/** One photo to show on the map. */
export interface PhotoMapItem {
    id: number;
    url: string;
    lat: number;
    lng: number;
    /** False for a photo the viewer may not reposition (someone else's upload). */
    movable?: boolean;
    caption?: string;
}

export interface PhotoMarkerLayerOptions {
    /**
     * Persists a moved photo. Resolve to commit; reject to have the marker snap
     * back to where it was. Omit to make every marker display-only.
     */
    onMove?: (id: number, lat: number, lng: number) => Promise<unknown>;
    /** Called when a marker is clicked (e.g. to open the lightbox). */
    onSelect?: (id: number) => void;
    /** Called when the pointer enters/leaves a marker, to sync an external grid. */
    onHover?: (id: number, on: boolean) => void;
    /** Right-click on a photo thumbnail (e.g. hide from map). */
    onContextMenu?: (id: number, event: L.LeafletMouseEvent) => void;
}

export interface PhotoMarkerLayer {
    /** The LayerGroup, so callers can add/remove it from a layers control. */
    layer: L.LayerGroup;
    /** Adds or replaces one photo's marker. */
    set: (item: PhotoMapItem) => void;
    /** Adds or replaces every given photo, dropping any marker not in the list. */
    replaceAll: (items: PhotoMapItem[]) => void;
    remove: (id: number) => void;
    /** Grows/shrinks one marker and reports whether it exists. */
    highlight: (id: number, on: boolean) => boolean;
    /** Bounds covering every current marker, or null when there are none. */
    bounds: () => L.LatLngBounds | null;
    count: () => number;
    destroy: () => void;
}

interface MarkerEntry {
    marker: L.Marker;
    url: string;
    lat: number;
    lng: number;
    highlighted: boolean;
}

/**
 * Creates a managed layer of draggable photo markers on *map*.
 *
 * A marker is only draggable when its item says `movable` **and** an `onMove`
 * handler was supplied - the server refuses to move another profile's photo, so
 * offering the drag would produce a move that always snaps back.
 *
 * @param map - The Leaflet map to attach to.
 * @param options - Reposition/selection callbacks; see PhotoMarkerLayerOptions.
 * @returns Handle for adding, removing, and highlighting markers.
 */
export function createPhotoMarkerLayer(map: L.Map, options: PhotoMarkerLayerOptions = {}): PhotoMarkerLayer {
    const layer = L.layerGroup().addTo(map);
    const markers = new Map<number, MarkerEntry>();

    function iconFor(entry: MarkerEntry): L.DivIcon {
        return makePhotoIcon(entry.url, photoMarkerSize(map.getZoom(), entry.highlighted), entry.highlighted);
    }

    function set(item: PhotoMapItem): void {
        const existing = markers.get(item.id);
        if (existing) layer.removeLayer(existing.marker);

        const draggable = !!item.movable && !!options.onMove;
        const marker = L.marker([item.lat, item.lng], {
            icon: makePhotoIcon(item.url, photoMarkerSize(map.getZoom(), false), false),
            draggable,
        });
        if (item.caption) marker.bindTooltip(item.caption, { direction: "top", className: "detail-pin-tooltip" });

        const entry: MarkerEntry = { marker, url: item.url, lat: item.lat, lng: item.lng, highlighted: false };

        if (draggable && options.onMove) {
            marker.on("dragend", () => {
                const pos = marker.getLatLng();
                const prevLat = entry.lat;
                const prevLng = entry.lng;
                entry.lat = pos.lat;
                entry.lng = pos.lng;
                options.onMove?.(item.id, pos.lat, pos.lng).catch(() => {
                    // Server refused the move - put the marker back where it was
                    // rather than leaving the map disagreeing with the database.
                    marker.setLatLng([prevLat, prevLng]);
                    entry.lat = prevLat;
                    entry.lng = prevLng;
                });
            });
        }
        if (options.onSelect) marker.on("click", () => options.onSelect?.(item.id));
        if (options.onHover) {
            marker.on("mouseover", () => options.onHover?.(item.id, true));
            marker.on("mouseout", () => options.onHover?.(item.id, false));
        }
        if (options.onContextMenu) {
            marker.on("contextmenu", (event: L.LeafletMouseEvent) => {
                L.DomEvent.stop(event);
                options.onContextMenu?.(item.id, event);
            });
        }

        marker.addTo(layer);
        markers.set(item.id, entry);
    }

    function remove(id: number): void {
        const entry = markers.get(id);
        if (!entry) return;
        layer.removeLayer(entry.marker);
        markers.delete(id);
    }

    function replaceAll(items: PhotoMapItem[]): void {
        const keep = new Set(items.map((item) => item.id));
        Array.from(markers.keys())
            .filter((id) => !keep.has(id))
            .forEach(remove);
        items.forEach(set);
    }

    function highlight(id: number, on: boolean): boolean {
        const entry = markers.get(id);
        if (!entry) return false;
        entry.highlighted = on;
        entry.marker.setIcon(iconFor(entry));
        // Highlight only changes the icon. Panning to the photo on hover would
        // jump the view under a marker that is already at these coordinates.
        return true;
    }

    // Re-scale every thumbnail on zoom so they keep a sane share of the view.
    const onZoom = (): void => {
        markers.forEach((entry) => entry.marker.setIcon(iconFor(entry)));
    };
    map.on("zoomend", onZoom);

    return {
        layer,
        set,
        replaceAll,
        remove,
        highlight,
        bounds: () => {
            if (!markers.size) return null;
            return L.latLngBounds(Array.from(markers.values()).map((entry) => [entry.lat, entry.lng] as [number, number]));
        },
        count: () => markers.size,
        destroy: () => {
            map.off("zoomend", onZoom);
            layer.clearLayers();
            map.removeLayer(layer);
            markers.clear();
        },
    };
}
