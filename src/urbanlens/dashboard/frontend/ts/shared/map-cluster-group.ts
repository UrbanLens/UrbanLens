/**
 * The container the main map's pins will live in once `map-page.ts` is ported, independent of
 * which engine draws them.
 *
 * **Nothing calls this yet.** `map-page.ts` and `map-annotations.ts` build
 * `createPinClusterGroup()` from `map-clusters.ts` against a raw `L.Map`, so this facade and its
 * MapLibre half run only under `bun test`. Fixing a bug here does not change what a browser draws;
 * `PL8` item 3 is where the porting work is tracked.
 *
 * Leaflet gets this from the `leaflet.markercluster` plugin; MapLibre has no equivalent, so
 * `maplibre-cluster-group.ts` rebuilds it on `supercluster` - the same library MapLibre clusters
 * GeoJSON sources with internally. Both sides draw the badge from `pinClusterIconParts`, so the
 * markup and `_map.scss` rules stay shared.
 *
 * One event is exposed rather than markercluster's four. `map-page.ts` listens for
 * `animationend spiderfied unspiderfied layeradd` for a single reason: those are the moments
 * markercluster rebuilds marker elements and drops the `.is-selected` class the page stamped on
 * them. `regroup` names that reason, so the MapLibre side can fire it at its own equivalent
 * moments instead of pretending to have markercluster's animation lifecycle.
 */

import { createPinClusterGroup, type PinClusterGroup, type PinClusterGroupOptions } from "./map-clusters";
import type { MapMarker } from "./map-markers";
import type { MapView } from "./map-view";

declare const L: typeof import("leaflet");

export interface ClusterGroupOptions {
    /** Cluster radius in pixels at a given zoom. */
    radiusForZoom?: (zoom: number) => number;
    /** Spread the members of a cluster that cannot be split by zooming any further. */
    spiderfyAtMaxZoom?: boolean;
    /** Render large batches in slices rather than one blocking pass. */
    chunked?: boolean;
    chunkSize?: number;
    chunkInterval?: number;
}

export interface MapClusterGroup {
    addTo(view: MapView): void;
    addLayer(marker: MapMarker): void;
    removeLayer(marker: MapMarker): void;
    addLayers(markers: MapMarker[]): void;
    removeLayers(markers: MapMarker[]): void;
    clearLayers(): void;
    /** Every marker in the group, clustered or not - the pin counter reads its length. */
    getLayers(): MapMarker[];
    hasLayer(marker: MapMarker): boolean;
    /**
     * Keeps a marker drawn but out of clustering, for the length of a drag: it must stay under the
     * cursor and must not be swept up by a regroup while the user is still holding it.
     */
    detach(marker: MapMarker): void;
    /** Returns a detached marker to clustering, at wherever it now sits. */
    reattach(marker: MapMarker): void;
    /** Fired whenever marker elements have been rebuilt and per-element state must be restamped. */
    on(event: "regroup", handler: () => void): void;
    remove(): void;
}

/** The markercluster options for a set of neutral ones. */
export function leafletClusterOptions(options: ClusterGroupOptions): PinClusterGroupOptions {
    return {
        ...(options.radiusForZoom ? { maxClusterRadius: options.radiusForZoom } : {}),
        spiderfyOnMaxZoom: options.spiderfyAtMaxZoom ?? true,
        showCoverageOnHover: false,
        animate: true,
        animateAddingMarkers: false,
        ...(options.chunked ? { chunkedLoading: true, chunkSize: options.chunkSize, chunkInterval: options.chunkInterval } : {}),
    };
}

/** The markercluster events that mean "elements were rebuilt"; `regroup` stands for all of them. */
const LEAFLET_REGROUP_EVENTS = "animationend spiderfied unspiderfied layeradd";

export function createLeafletClusterGroup(options: ClusterGroupOptions = {}, map?: L.Map): MapClusterGroup {
    let group: PinClusterGroup | null = map ? createPinClusterGroup(leafletClusterOptions(options), map) : null;
    /** Kept by façade so `getLayers` can answer in façade terms rather than Leaflet's. */
    const held = new Map<unknown, MapMarker>();
    const pending: (() => void)[] = [];
    let attachedTo: MapView | null = null;

    function ready(run: (group: PinClusterGroup) => void): void {
        if (group) run(group);
        else pending.push(() => run(group!));
    }

    return {
        addTo: (view) => {
            const native = view.native as L.Map;
            attachedTo = view;
            group ??= createPinClusterGroup(leafletClusterOptions(options), native);
            group.addTo(native);
            for (const run of pending.splice(0)) run();
        },
        addLayer: (marker) => {
            held.set(marker.native, marker);
            ready((live) => void live.addLayer(marker.native as L.Layer));
        },
        removeLayer: (marker) => {
            held.delete(marker.native);
            ready((live) => void live.removeLayer(marker.native as L.Layer));
        },
        addLayers: (markers) => {
            for (const marker of markers) held.set(marker.native, marker);
            ready((live) => void live.addLayers(markers.map((marker) => marker.native as L.Layer)));
        },
        removeLayers: (markers) => {
            for (const marker of markers) held.delete(marker.native);
            ready((live) => void live.removeLayers(markers.map((marker) => marker.native as L.Layer)));
        },
        clearLayers: () => {
            held.clear();
            ready((live) => void live.clearLayers());
        },
        getLayers: () => [...held.values()],
        hasLayer: (marker) => held.has(marker.native),
        detach: (marker) =>
            ready((live) => {
                live.removeLayer(marker.native as L.Layer);
                if (attachedTo) marker.addTo(attachedTo);
            }),
        reattach: (marker) =>
            ready((live) => {
                marker.remove();
                live.addLayer(marker.native as L.Layer);
            }),
        on: (_event, handler) => ready((live) => void live.on(LEAFLET_REGROUP_EVENTS, handler)),
        remove: () => {
            held.clear();
            group?.remove();
        },
    };
}
