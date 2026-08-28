/**
 * Shared Leaflet.markercluster helpers used by every map that groups nearby
 * pins: the main map's inline cluster layer, and the pin-detail / wiki maps.
 *
 * Pin-detail and wiki maps zoom in close enough that neighbouring buildings
 * must stay independently clickable. Clustering therefore collapses to a 1px
 * radius from zoom 18 up - only coincident markers still group (and can
 * spiderfy). Photos have their own radius in photo-map.ts, because same-spot
 * GPS hits should stay stacked at every zoom.
 */

declare const L: typeof import("leaflet");

/** The subset of leaflet.markercluster's cluster object this file reads. */
interface ClusterLike {
    getChildCount(): number;
    getAllChildMarkers(): L.Marker[];
}

type MarkerClusterFactory = (options: {
    maxClusterRadius?: number | ((zoom: number) => number);
    spiderfyOnMaxZoom?: boolean;
    showCoverageOnHover?: boolean;
    zoomToBoundsOnClick?: boolean;
    animate?: boolean;
    animateAddingMarkers?: boolean;
    iconCreateFunction?: (cluster: ClusterLike) => L.DivIcon;
}) => L.LayerGroup;

function markerClusterFactory(): MarkerClusterFactory | undefined {
    const fn = (L as unknown as { markerClusterGroup?: MarkerClusterFactory }).markerClusterGroup;
    return typeof fn === "function" ? fn : undefined;
}

/** Pixel sizes of .pin-cluster--{s,m,l} in _map.scss - keep in lockstep. */
const PIN_CLUSTER_PX = { s: 34, m: 42, l: 50 } as const;

/**
 * Cluster radius for child pins on a pin-detail or wiki map.
 *
 * Mirrors the main map's "auto" radius at mid zooms, then drops to 1px once
 * individual buildings are on screen so a campus of close footprints never
 * hides its markers behind a badge.
 *
 * @param zoom - The map's current zoom level.
 */
export function detailPinClusterRadius(zoom: number): number {
    if (zoom >= 18) return 1;
    if (zoom <= 14) return 40;
    if (zoom <= 16) return 20;
    return 8;
}

/**
 * Markup + edge length for the numbered pin-cluster badge.
 *
 * @param count - How many markers this cluster represents.
 */
export function pinClusterIconParts(count: number): { html: string; size: number } {
    const siz: keyof typeof PIN_CLUSTER_PX = count < 10 ? "s" : count < 100 ? "m" : "l";
    const size = PIN_CLUSTER_PX[siz];
    return {
        html: `<div class="pin-cluster pin-cluster--${siz}"><span>${count}</span></div>`,
        size,
    };
}

/** Whether the CDN plugin has attached `L.markerClusterGroup`. */
export function hasMarkerCluster(): boolean {
    return markerClusterFactory() !== undefined;
}

/**
 * A MarkerClusterGroup that uses the same numbered badge as the main map.
 *
 * Falls back to a plain LayerGroup when the plugin is not on the page, so
 * callers can add/remove markers identically either way.
 *
 * @param options - Extra cluster-group options (merged over the defaults).
 */
export function createPinClusterGroup(
    options: {
        maxClusterRadius?: number | ((zoom: number) => number);
        spiderfyOnMaxZoom?: boolean;
        showCoverageOnHover?: boolean;
        zoomToBoundsOnClick?: boolean;
        animate?: boolean;
        animateAddingMarkers?: boolean;
        iconCreateFunction?: (cluster: ClusterLike) => L.DivIcon;
    } = {},
): L.LayerGroup {
    const factory = markerClusterFactory();
    if (!factory) return L.layerGroup();
    return factory({
        maxClusterRadius: detailPinClusterRadius,
        spiderfyOnMaxZoom: true,
        showCoverageOnHover: false,
        animate: true,
        animateAddingMarkers: false,
        iconCreateFunction(cluster) {
            const { html, size } = pinClusterIconParts(cluster.getChildCount());
            return L.divIcon({ html, className: "", iconSize: [size, size] });
        },
        ...options,
    });
}

/**
 * Pull a marker out of a cluster group for the length of a drag, then put it
 * back. Leaflet.markercluster does not update clusters while a member is being
 * dragged - the main map already does this dance for root pins.
 *
 * @param marker - The marker that may be dragged.
 * @param group - Cluster group (or plain LayerGroup fallback) that owns it.
 * @param map - The map, so the marker can sit on it mid-drag.
 * @param skip - When this returns true, leave the marker in the group (e.g. select mode).
 */
export function reclusterOnDrag(marker: L.Marker, group: L.LayerGroup, map: L.Map, skip?: () => boolean): void {
    marker.on("dragstart", () => {
        if (skip?.()) return;
        group.removeLayer(marker);
        marker.addTo(map);
    });
}

/** Put a marker that was pulled out for dragging back into its cluster group. */
export function returnToCluster(marker: L.Marker, group: L.LayerGroup, map: L.Map): void {
    if (map.hasLayer(marker)) map.removeLayer(marker);
    group.addLayer(marker);
}

/**
 * True when the Leaflet (or native) mouse event was a ctrl/cmd click.
 *
 * Used to start additive multi-select from a second click without first
 * arming the select tool.
 *
 * @param event - A Leaflet mouse event or a native MouseEvent.
 */
export function isAdditiveClick(event: { originalEvent?: { ctrlKey?: boolean; metaKey?: boolean }; ctrlKey?: boolean; metaKey?: boolean }): boolean {
    const src = event.originalEvent ?? event;
    return !!(src.ctrlKey || src.metaKey);
}

/**
 * Remembers the last plain-clicked id so a subsequent modifier-click can
 * start a multi-selection that includes both.
 */
export class AdditiveSelectMemory {
    private lastId: string | null = null;

    /** Record a plain click on this id. */
    remember(id: string): void {
        this.lastId = id;
    }

    /** Drop the remembered id (select mode exited, or the map was reset). */
    clear(): void {
        this.lastId = null;
    }

    /**
     * Ids that should become selected when the user modifier-clicks `id`
     * outside of select mode. Always includes `id`; includes the last
     * remembered id when it is a different thing.
     *
     * @param id - The id that was just modifier-clicked.
     */
    idsForAdditiveStart(id: string): string[] {
        if (this.lastId && this.lastId !== id) return [this.lastId, id];
        return [id];
    }
}
