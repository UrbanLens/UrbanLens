/**
 * Photo markers on a Leaflet map: the thumbnail icon, its zoom-dependent size, drag-to-reposition, and nearby-photo clustering.
 */

declare const L: typeof import("leaflet");

import { canCluster } from "./map-clusters";
import { escHtml } from "./escape-html";

/** The subset of leaflet.markercluster's cluster object this file reads. */
interface PhotoClusterLike {
    getAllChildMarkers(): L.Marker[];
}

/** The parts of leaflet.markercluster's group used to redraw the cluster a photo is hidden in. */
interface PhotoClusterGroupLike {
    getVisibleParent(marker: L.Marker): L.Marker | null;
    refreshClusters(marker: L.Marker): unknown;
}

type PhotoClusterFactory = (options: {
    maxClusterRadius?: number | ((zoom: number) => number);
    showCoverageOnHover?: boolean;
    spiderfyOnMaxZoom?: boolean;
    zoomToBoundsOnClick?: boolean;
    animate?: boolean;
    animateAddingMarkers?: boolean;
    iconCreateFunction?: (cluster: PhotoClusterLike) => L.DivIcon;
}) => L.LayerGroup;

/** Base thumbnail size at zoom 16 and above. */
export const PHOTO_MARKER_BASE_SIZE = 44;
/** Floor for the zoomed-far-out case, below which a thumbnail is unreadable. */
export const PHOTO_MARKER_MIN_SIZE = 14;
/** How long {@link PhotoMarkerLayer.flash} marks a photo, in milliseconds. */
export const PHOTO_MARKER_FLASH_MS = 2400;
/** Lifts a highlighted photo above the photos it overlaps. */
const HIGHLIGHT_Z_OFFSET = 1000;
/** Resting offset of the back photo in a cluster, in pixels. Must match _gallery.scss. */
export const PHOTO_CLUSTER_PEEK = 7;


/**
 * Edge length for a photo marker at the given zoom.
 *
 * Full size at zoom 16+, halving every 4 zoom levels below that so thumbnails
 * don't cover a huge chunk of the map when zoomed far out; never smaller than
 * PHOTO_MARKER_MIN_SIZE.
 *
 * @param zoom - The map's current zoom level.
 */
export function photoMarkerSize(zoom: number): number {
    const scale = 2 ** ((zoom - 16) * 0.5);
    return Math.max(PHOTO_MARKER_MIN_SIZE, Math.min(PHOTO_MARKER_BASE_SIZE, Math.round(PHOTO_MARKER_BASE_SIZE * scale)));
}

/** How a photo marker is drawn: hovered or selected, and whether it is mid-flash. */
export interface PhotoMarkerState {
    highlighted?: boolean;
    flashing?: boolean;
}

function stateClasses(base: string, state: PhotoMarkerState): string {
    return [base, state.highlighted || state.flashing ? "is-highlighted" : "", state.flashing ? "is-flashing" : ""].filter(Boolean).join(" ");
}

/**
 * Cluster radius for photo markers: group them when their thumbnails would
 * overlap, so the stacked badge replaces a pile of unreadable squares.
 *
 * @param zoom - The map's current zoom level.
 */
export function photoClusterRadius(zoom: number): number {
    return Math.max(PHOTO_MARKER_MIN_SIZE, Math.round(photoMarkerSize(zoom) * 0.9));
}

/**
 * The square thumbnail icon used for a photo on any map. Its look, including the highlight, is `.photo-marker` in _gallery.scss.
 *
 * @param url - Image URL to show in the marker.
 * @param size - Edge length in pixels (see photoMarkerSize).
 * @param state - Highlight and flash state to draw.
 */
export function makePhotoIcon(url: string, size: number, state: PhotoMarkerState = {}): L.DivIcon {
    return L.divIcon({
        className: stateClasses("photo-marker", state),
        html: `<img src="${escHtml(url)}" class="photo-marker-img" alt="" draggable="false" style="width:${size}px;height:${size}px">`,
        iconSize: [size, size],
        iconAnchor: [size / 2, size / 2],
    });
}

/**
 * Markup for a stacked-photo cluster badge. Two images, the front fully
 * visible, the back peeking out as a border; a count pill for the collection.
 *
 * Pure HTML so tests can assert on the stack without constructing a DivIcon.
 *
 * @param frontUrl - The top photo (highest id / most recent).
 * @param backUrl - The photo immediately behind it; falls back to frontUrl.
 * @param count - Total photos in the cluster (shown in the pill).
 * @param size - Edge length of each thumbnail, matching photoMarkerSize.
 */
export function photoClusterMarkup(frontUrl: string, backUrl: string, count: number, size: number): string {
    const front = escHtml(frontUrl);
    const back = escHtml(backUrl || frontUrl);
    return `<div class="photo-cluster" style="--pcs:${size}px" aria-label="${count} photos">
        <img class="photo-cluster__img photo-cluster__img--back" src="${back}" alt="" draggable="false">
        <img class="photo-cluster__img photo-cluster__img--front" src="${front}" alt="" draggable="false">
        <span class="photo-cluster__count">${count}</span>
    </div>`;
}

/**
 * Leaflet icon wrapping {@link photoClusterMarkup}. Sized to include the back
 * photo's peek so the cluster doesn't clip at rest; hover fans further via
 * overflow:visible.
 *
 * @param frontUrl - The top photo.
 * @param backUrl - The photo behind it.
 * @param count - Total photos in the cluster.
 * @param size - Thumbnail edge length.
 * @param state - Highlight and flash state of the front photo.
 */
export function makePhotoClusterIcon(frontUrl: string, backUrl: string, count: number, size: number, state: PhotoMarkerState = {}): L.DivIcon {
    const peek = PHOTO_CLUSTER_PEEK;
    const iconSize = size + peek + 4;
    return L.divIcon({
        className: stateClasses("photo-cluster-icon", state),
        html: photoClusterMarkup(frontUrl, backUrl, count, size),
        iconSize: [iconSize, iconSize],
        iconAnchor: [size / 2, size / 2],
    });
}

/** Marker tagged with the photo it represents, so cluster icons can read URLs and which photo to put on top. */
export interface TaggedPhotoMarker extends L.Marker {
    _ulPhotoUrl?: string;
    _ulPhotoId?: number;
    _ulHighlighted?: boolean;
    _ulFlashing?: boolean;
}

function isLit(marker: TaggedPhotoMarker): boolean {
    return !!(marker._ulHighlighted || marker._ulFlashing);
}

/** Stash the photo identity on a marker for {@link createPhotoClusterGroup}. */
export function tagPhotoMarker(marker: L.Marker, url: string, id: number): void {
    const tagged = marker as TaggedPhotoMarker;
    tagged._ulPhotoUrl = url;
    tagged._ulPhotoId = id;
}

/**
 * A MarkerClusterGroup whose clusters render as a stacked pair of thumbnails, a highlighted photo on top.
 *
 * Falls back to a plain LayerGroup when leaflet.markercluster is not loaded.
 *
 * @param map - Used to size cluster icons at the current zoom.
 */
export function createPhotoClusterGroup(map: L.Map): L.LayerGroup {
    if (!canCluster(map)) return L.layerGroup();
    const factory = (L as unknown as { markerClusterGroup: PhotoClusterFactory }).markerClusterGroup;
    return factory({
        maxClusterRadius: photoClusterRadius,
        showCoverageOnHover: false,
        spiderfyOnMaxZoom: true,
        zoomToBoundsOnClick: true,
        animate: true,
        animateAddingMarkers: false,
        iconCreateFunction(cluster) {
            const markers = (cluster.getAllChildMarkers() as TaggedPhotoMarker[]).slice();
            markers.sort((a, b) => Number(isLit(b)) - Number(isLit(a)) || (b._ulPhotoId ?? 0) - (a._ulPhotoId ?? 0));
            const top = markers[0];
            const front = top?._ulPhotoUrl ?? "";
            const back = markers[1]?._ulPhotoUrl ?? front;
            return makePhotoClusterIcon(front, back, markers.length, photoMarkerSize(map.getZoom()), { highlighted: !!top?._ulHighlighted, flashing: !!top?._ulFlashing });
        },
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
    /** Marks or unmarks one photo (hover, selection) and reports whether it exists. */
    highlight: (id: number, on: boolean) => boolean;
    /** Marks one photo with an animation for a moment, so the eye finds it after a pan; reports whether it exists. */
    flash: (id: number, durationMs?: number) => boolean;
    /** Bounds covering every current marker, or null when there are none. */
    bounds: () => L.LatLngBounds | null;
    count: () => number;
    destroy: () => void;
}

interface MarkerEntry {
    marker: TaggedPhotoMarker;
    url: string;
    lat: number;
    lng: number;
    highlighted: boolean;
    flashing: boolean;
    flashTimer?: ReturnType<typeof setTimeout>;
}

/**
 * Creates a managed layer of draggable photo markers on *map*.
 *
 * A marker is only draggable when its item says `movable` **and** an `onMove`
 * handler was supplied - the server refuses to move another profile's photo, so
 * offering the drag would produce a move that always snaps back.
 *
 * Nearby photos cluster into a stacked badge when leaflet.markercluster is on
 * the page (pin detail, wiki, albums on those pages).
 *
 * @param map - The Leaflet map to attach to.
 * @param options - Reposition/selection callbacks; see PhotoMarkerLayerOptions.
 * @returns Handle for adding, removing, and highlighting markers.
 */
export function createPhotoMarkerLayer(map: L.Map, options: PhotoMarkerLayerOptions = {}): PhotoMarkerLayer {
    const layer = createPhotoClusterGroup(map).addTo(map);
    const clusters = layer as Partial<PhotoClusterGroupLike>;
    const markers = new Map<number, MarkerEntry>();

    function iconFor(entry: MarkerEntry): L.DivIcon {
        return makePhotoIcon(entry.url, photoMarkerSize(map.getZoom()), { highlighted: entry.highlighted, flashing: entry.flashing });
    }

    /** Applies the entry's state to the element already drawn: replacing it would end a drag and drop a click under the pointer. */
    function paint(entry: MarkerEntry): void {
        const { marker } = entry;
        const lit = entry.highlighted || entry.flashing;
        marker._ulHighlighted = entry.highlighted;
        marker._ulFlashing = entry.flashing;
        marker.options.icon = iconFor(entry);
        const element = marker.getElement();
        element?.classList.toggle("is-highlighted", lit);
        element?.classList.toggle("is-flashing", entry.flashing);
        marker.setZIndexOffset(lit ? HIGHLIGHT_Z_OFFSET : 0);
        if (clusters.getVisibleParent && clusters.refreshClusters && clusters.getVisibleParent(marker) !== marker) clusters.refreshClusters(marker);
    }

    function set(item: PhotoMapItem): void {
        const existing = markers.get(item.id);
        if (existing) {
            clearTimeout(existing.flashTimer);
            layer.removeLayer(existing.marker);
        }

        const draggable = !!item.movable && !!options.onMove;
        const marker: TaggedPhotoMarker = L.marker([item.lat, item.lng], {
            icon: makePhotoIcon(item.url, photoMarkerSize(map.getZoom())),
            draggable,
        });
        tagPhotoMarker(marker, item.url, item.id);
        if (item.caption) marker.bindTooltip(escHtml(item.caption), { direction: "top", className: "detail-pin-tooltip" });

        const entry: MarkerEntry = { marker, url: item.url, lat: item.lat, lng: item.lng, highlighted: false, flashing: false };

        // markercluster re-files a dragged child itself on dragend.
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
        clearTimeout(entry.flashTimer);
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
        paint(entry);
        return true;
    }

    function flash(id: number, durationMs: number = PHOTO_MARKER_FLASH_MS): boolean {
        const entry = markers.get(id);
        if (!entry) return false;
        clearTimeout(entry.flashTimer);
        if (entry.flashing) {
            // Restarts the animation: re-adding a class in the same frame does not.
            entry.flashing = false;
            paint(entry);
            void entry.marker.getElement()?.offsetWidth;
        }
        entry.flashing = true;
        paint(entry);
        entry.flashTimer = setTimeout(() => {
            entry.flashing = false;
            paint(entry);
        }, durationMs);
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
        flash,
        bounds: () => {
            if (!markers.size) return null;
            return L.latLngBounds(Array.from(markers.values()).map((entry) => [entry.lat, entry.lng] as [number, number]));
        },
        count: () => markers.size,
        destroy: () => {
            markers.forEach((entry) => clearTimeout(entry.flashTimer));
            map.off("zoomend", onZoom);
            layer.clearLayers();
            map.removeLayer(layer);
            markers.clear();
        },
    };
}
