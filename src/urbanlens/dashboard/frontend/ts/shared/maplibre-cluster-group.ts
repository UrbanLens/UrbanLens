/**
 * The MapLibre half of `map-cluster-group.ts`, on `supercluster` - the library MapLibre already
 * uses to cluster a GeoJSON source, driven directly here because the pins are DOM markers with
 * their own popups and drag behaviour, not points in a source.
 *
 * The shape of the work each frame: ask supercluster what the current viewport and zoom resolve to,
 * show the markers it returns as individual points, draw a numbered badge for each cluster, and
 * take everything else off the map. Only what is on screen is ever materialised, which is what the
 * lazy marker in `maplibre-markers.ts` exists for - an account with 30,000 pins draws a few hundred.
 *
 * Two deliberate differences from `leaflet.markercluster`, both named rather than papered over:
 *
 * - **Radius by zoom.** markercluster re-evaluates `maxClusterRadius` on every regroup; supercluster
 *   bakes one radius into the index it builds. The policy function is therefore evaluated at the
 *   current zoom and the index rebuilt only when the answer changes. The main map's policy is a
 *   three-step function, so that is at most three builds for the whole zoom range.
 * - **No spiderfy.** markercluster fans a cluster's members onto a circle when zooming can no longer
 *   separate them. Nothing here does that yet: clicking a cluster zooms to the point where
 *   supercluster splits it, and pins at identical coordinates will still overlap at maximum zoom.
 */

import Supercluster from "supercluster";

import { pinClusterIconParts } from "./map-clusters";
import type { ClusterGroupOptions, MapClusterGroup } from "./map-cluster-group";
import type { MapMarker } from "./map-markers";
import type { MapView } from "./map-view";

import type { Marker as MaplibreMarker } from "maplibre-gl";

declare const maplibregl: typeof import("maplibre-gl");

/** Index into the marker list a clustered point stands for. */
interface PointProps {
    at: number;
}

const DEFAULT_RADIUS = 40;
/** Beyond this, supercluster returns individual points - the map's own maxZoom is 21. */
const CLUSTER_MAX_ZOOM = 20;

export function createMaplibreClusterGroup(options: ClusterGroupOptions = {}): MapClusterGroup {
    const held = new Set<MapMarker>();
    const regroupListeners: (() => void)[] = [];

    let view: MapView | null = null;
    let index: Supercluster<PointProps> | null = null;
    let indexedRadius: number | null = null;
    /** Parallel to the points loaded into `index`; `PointProps.at` indexes it. */
    let indexed: MapMarker[] = [];

    const shown = new Set<MapMarker>();
    const badges = new Map<number, MaplibreMarker>();
    let scheduled = false;

    function radiusAt(zoom: number): number {
        return options.radiusForZoom ? options.radiusForZoom(zoom) : DEFAULT_RADIUS;
    }

    function reindex(radius: number): void {
        indexed = [...held];
        index = new Supercluster<PointProps>({ radius, maxZoom: CLUSTER_MAX_ZOOM, minPoints: 2 });
        index.load(
            indexed.map((marker, at) => {
                const position = marker.getLatLng();
                return { type: "Feature" as const, properties: { at }, geometry: { type: "Point" as const, coordinates: [position.lng, position.lat] } };
            }),
        );
        indexedRadius = radius;
    }

    function badgeFor(count: number, onClick: () => void): MaplibreMarker {
        const { html, size } = pinClusterIconParts(count);
        const element = document.createElement("div");
        element.innerHTML = html;
        element.style.width = `${size}px`;
        element.style.height = `${size}px`;
        element.style.cursor = "pointer";
        element.addEventListener("click", (event) => {
            event.stopPropagation();
            onClick();
        });
        return new maplibregl.Marker({ element, anchor: "center" });
    }

    function clearBadges(keep: Set<number>): void {
        for (const [id, badge] of badges) {
            if (keep.has(id)) continue;
            badge.remove();
            badges.delete(id);
        }
    }

    function rebuild(): void {
        if (!view) return;
        const zoom = view.getZoom();
        const radius = radiusAt(zoom);
        // A membership change invalidates the index as surely as a radius change does.
        if (!index || indexedRadius !== radius || indexed.length !== held.size) reindex(radius);
        const bounds = view.getBounds();
        const found = index!.getClusters([bounds.west, bounds.south, bounds.east, bounds.north], Math.floor(zoom));

        const wanted = new Set<MapMarker>();
        const keptBadges = new Set<number>();
        for (const feature of found) {
            const [lng, lat] = feature.geometry.coordinates as [number, number];
            const properties = feature.properties as { cluster?: boolean; cluster_id?: number; point_count?: number; at?: number };
            if (properties.cluster) {
                const id = properties.cluster_id!;
                keptBadges.add(id);
                if (badges.has(id)) {
                    badges.get(id)!.setLngLat([lng, lat]);
                    continue;
                }
                const badge = badgeFor(properties.point_count ?? 0, () => {
                    const expansion = index!.getClusterExpansionZoom(id);
                    view!.setView({ lat, lng }, Math.min(expansion, view!.getMaxZoom()));
                });
                badge.setLngLat([lng, lat]).addTo(view.native as import("maplibre-gl").Map);
                badges.set(id, badge);
                continue;
            }
            const marker = indexed[properties.at!];
            if (marker) wanted.add(marker);
        }

        clearBadges(keptBadges);
        for (const marker of shown) {
            if (wanted.has(marker)) continue;
            marker.remove();
            shown.delete(marker);
        }
        for (const marker of wanted) {
            if (shown.has(marker)) continue;
            marker.addTo(view);
            shown.add(marker);
        }
        // Elements are new for every marker that just appeared, so anything the page stamped on
        // them (the selection class) has to be stamped again.
        for (const listener of regroupListeners) listener();
    }

    function schedule(): void {
        if (scheduled || !view) return;
        scheduled = true;
        // Coalesces a whole synchronous batch of addLayers/removeLayers into one pass.
        queueMicrotask(() => {
            scheduled = false;
            rebuild();
        });
    }

    return {
        addTo: (target) => {
            view = target;
            target.on("moveend", schedule);
            target.on("zoomend", schedule);
            schedule();
        },
        addLayer: (marker) => {
            held.add(marker);
            index = null;
            schedule();
        },
        removeLayer: (marker) => {
            held.delete(marker);
            if (shown.delete(marker)) marker.remove();
            index = null;
            schedule();
        },
        addLayers: (markers) => {
            for (const marker of markers) held.add(marker);
            index = null;
            schedule();
        },
        removeLayers: (markers) => {
            for (const marker of markers) {
                held.delete(marker);
                if (shown.delete(marker)) marker.remove();
            }
            index = null;
            schedule();
        },
        clearLayers: () => {
            for (const marker of shown) marker.remove();
            shown.clear();
            held.clear();
            index = null;
            schedule();
        },
        getLayers: () => [...held],
        hasLayer: (marker) => held.has(marker),
        on: (_event, handler) => void regroupListeners.push(handler),
        remove: () => {
            if (view) {
                view.off("moveend", schedule);
                view.off("zoomend", schedule);
            }
            for (const marker of shown) marker.remove();
            shown.clear();
            clearBadges(new Set());
            held.clear();
            index = null;
            view = null;
        },
    };
}
