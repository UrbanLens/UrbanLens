/**
 * The Leaflet group is a thin delegation to `leaflet.markercluster`, so what is worth asserting
 * there is that batches stay batches and that `regroup` really subscribes to the events that mean
 * "elements were rebuilt". The MapLibre group has no plugin under it at all, so it is tested
 * against real `supercluster` output: what the viewport shows, what it drops, and what collapses.
 */
import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { createLeafletClusterGroup, leafletClusterOptions, type MapClusterGroup } from "./map-cluster-group";
import { createMaplibreClusterGroup } from "./maplibre-cluster-group";
import { createMaplibreMarker } from "./maplibre-markers";
import type { MapMarker, MarkerIcon } from "./map-markers";
import type { LatLng, MapView, MapViewBounds } from "./map-view";

const ICON: MarkerIcon = { html: "<span>pin</span>", className: "map-pin-icon-wrap", size: [28, 28], anchor: [14, 14] };

const realL = (globalThis as Record<string, unknown>).L;
const realMaplibregl = (globalThis as Record<string, unknown>).maplibregl;

afterEach(() => {
    if (realL === undefined) delete (globalThis as Record<string, unknown>).L;
    else (globalThis as Record<string, unknown>).L = realL;
    if (realMaplibregl === undefined) delete (globalThis as Record<string, unknown>).maplibregl;
    else (globalThis as Record<string, unknown>).maplibregl = realMaplibregl;
});

// -- Leaflet ------------------------------------------------------------------------------------

describe("leafletClusterOptions", () => {
    test("passes a zoom-varying radius through as markercluster's own option", () => {
        const policy = (zoom: number): number => (zoom <= 10 ? 60 : 10);
        expect(leafletClusterOptions({ radiusForZoom: policy }).maxClusterRadius).toBe(policy);
    });

    test("leaves the radius unset when no policy was given, so the helper's default stands", () => {
        expect("maxClusterRadius" in leafletClusterOptions({})).toBe(false);
    });

    test("only asks for chunking when chunking was asked for", () => {
        expect(leafletClusterOptions({}).chunkedLoading).toBeUndefined();
        expect(leafletClusterOptions({ chunked: true, chunkSize: 400, chunkInterval: 60 })).toMatchObject({ chunkedLoading: true, chunkSize: 400, chunkInterval: 60 });
    });

    test("spiderfies at max zoom unless told not to", () => {
        expect(leafletClusterOptions({}).spiderfyOnMaxZoom).toBe(true);
        expect(leafletClusterOptions({ spiderfyAtMaxZoom: false }).spiderfyOnMaxZoom).toBe(false);
    });
});

interface LeafletClusterStub {
    calls: string[];
    events: string[];
    layers: unknown[];
}

function stubLeafletCluster(): LeafletClusterStub {
    const state: LeafletClusterStub = { calls: [], events: [], layers: [] };
    const group = {
        addTo: () => void state.calls.push("addTo"),
        addLayer: (layer: unknown) => void (state.calls.push("addLayer"), state.layers.push(layer)),
        removeLayer: (layer: unknown) => {
            state.calls.push("removeLayer");
            state.layers.splice(state.layers.indexOf(layer), 1);
        },
        addLayers: (layers: unknown[]) => void (state.calls.push(`addLayers:${layers.length}`), state.layers.push(...layers)),
        removeLayers: (layers: unknown[]) => {
            state.calls.push(`removeLayers:${layers.length}`);
            for (const layer of layers) state.layers.splice(state.layers.indexOf(layer), 1);
        },
        clearLayers: () => void (state.calls.push("clearLayers"), (state.layers.length = 0)),
        on: (type: string) => void state.events.push(type),
        remove: () => void state.calls.push("remove"),
    };
    (globalThis as Record<string, unknown>).L = { markerClusterGroup: () => group, layerGroup: () => group, divIcon: (opts: unknown) => opts };
    return state;
}

/** A marker façade standing in for a real one - the cluster group only reads `native` and position. */
function fakeMarker(position: LatLng): MapMarker {
    const native = { id: `${position.lat},${position.lng}` };
    return { native, getLatLng: () => position } as unknown as MapMarker;
}

const leafletMap = { getMaxZoom: () => 21 } as unknown as L.Map;
const leafletView = { kind: "leaflet", native: leafletMap } as unknown as MapView;

describe("Leaflet cluster group", () => {
    let stub: LeafletClusterStub;
    let group: MapClusterGroup;

    beforeEach(() => {
        stub = stubLeafletCluster();
        group = createLeafletClusterGroup({}, leafletMap);
        group.addTo(leafletView);
    });

    test("adds a batch as one call, not one per marker", () => {
        group.addLayers([fakeMarker({ lat: 1, lng: 1 }), fakeMarker({ lat: 2, lng: 2 }), fakeMarker({ lat: 3, lng: 3 })]);
        expect(stub.calls).toEqual(["addTo", "addLayers:3"]);
    });

    test("hands markercluster the engine's own layers, not the façades", () => {
        const marker = fakeMarker({ lat: 1, lng: 1 });
        group.addLayer(marker);
        expect(stub.layers).toEqual([marker.native]);
    });

    test("answers its membership in façade terms", () => {
        const first = fakeMarker({ lat: 1, lng: 1 });
        const second = fakeMarker({ lat: 2, lng: 2 });
        group.addLayers([first, second]);
        expect(group.getLayers()).toEqual([first, second]);
        expect(group.hasLayer(first)).toBe(true);
        group.removeLayer(first);
        expect(group.getLayers()).toEqual([second]);
        expect(group.hasLayer(first)).toBe(false);
    });

    test("forgets everything when cleared", () => {
        group.addLayers([fakeMarker({ lat: 1, lng: 1 })]);
        group.clearLayers();
        expect(group.getLayers()).toEqual([]);
        expect(stub.calls).toContain("clearLayers");
    });

    test("subscribes regroup to every moment markercluster rebuilds elements", () => {
        // A selection class stamped on a marker element is lost on each of these; missing one
        // leaves the highlight silently dropped after that interaction.
        group.on("regroup", () => undefined);
        expect(stub.events).toEqual(["animationend spiderfied unspiderfied layeradd"]);
    });

    test("replays work queued before it was ever put on a map", () => {
        const late = createLeafletClusterGroup({});
        const marker = fakeMarker({ lat: 5, lng: 5 });
        late.addLayer(marker);
        expect(late.getLayers()).toEqual([marker]);
        late.addTo(leafletView);
        expect(stub.calls).toContain("addLayer");
    });
});

// -- MapLibre -----------------------------------------------------------------------------------

interface MaplibreStub {
    markers: { element: HTMLElement; lngLat: [number, number] | null; onMap: boolean }[];
}

function stubMaplibre(): MaplibreStub {
    const state: MaplibreStub = { markers: [] };
    class Marker {
        element: HTMLElement;
        lngLat: [number, number] | null = null;
        onMap = false;
        options: Record<string, unknown>;
        constructor(options: Record<string, unknown> = {}) {
            if (!(options.element instanceof HTMLElement)) throw new Error("maplibregl.Marker needs an element");
            this.element = options.element;
            this.options = options;
            state.markers.push(this);
        }
        setLngLat(value: [number, number]): this {
            this.lngLat = value;
            return this;
        }
        setOffset(): this {
            return this;
        }
        setDraggable(): this {
            return this;
        }
        addTo(): this {
            this.onMap = true;
            return this;
        }
        remove(): this {
            this.onMap = false;
            return this;
        }
        on(): this {
            return this;
        }
    }
    (globalThis as Record<string, unknown>).maplibregl = { Marker, Popup: class {} };
    return state;
}

interface FakeView extends MapView {
    zoom: number;
    bounds: MapViewBounds;
    setViewCalls: { center: LatLng; zoom?: number }[];
    fire(event: "moveend" | "zoomend"): void;
    listenerCount(): number;
}

function fakeView(zoom = 2, bounds: MapViewBounds = { south: -85, west: -180, north: 85, east: 180 }, maxZoom = 21): FakeView {
    const listeners: { event: string; handler: () => void }[] = [];
    const view = {
        kind: "maplibre" as const,
        native: { id: "map" },
        zoom,
        bounds,
        setViewCalls: [] as { center: LatLng; zoom?: number }[],
        getZoom: () => view.zoom,
        getMaxZoom: () => maxZoom,
        getMinZoom: () => 2,
        getBounds: () => view.bounds,
        setView: (center: LatLng, to?: number) => void view.setViewCalls.push({ center, zoom: to }),
        on: (event: string, handler: () => void) => void listeners.push({ event, handler }),
        off: (event: string, handler: () => void) => {
            const at = listeners.findIndex((listener) => listener.event === event && listener.handler === handler);
            if (at >= 0) listeners.splice(at, 1);
        },
        fire: (event: string) => {
            for (const listener of [...listeners]) if (listener.event === event) listener.handler();
        },
        listenerCount: () => listeners.length,
    };
    return view as unknown as FakeView;
}

/** Lets the microtask that coalesces a batch of mutations run. */
async function settle(): Promise<void> {
    await Promise.resolve();
    await Promise.resolve();
}

/** Badge elements currently on the map, by the count each shows. */
function badgeCounts(stub: MaplibreStub): number[] {
    return stub.markers
        .filter((marker) => marker.onMap && marker.element.querySelector(".pin-cluster"))
        .map((marker) => Number(marker.element.querySelector(".pin-cluster span")?.textContent ?? "0"))
        .sort((a, b) => a - b);
}

describe("MapLibre cluster group", () => {
    let stub: MaplibreStub;

    beforeEach(() => {
        stub = stubMaplibre();
    });

    function marker(position: LatLng): MapMarker {
        return createMaplibreMarker(position, { icon: ICON });
    }

    test("draws the markers the viewport contains", async () => {
        const group = createMaplibreClusterGroup();
        const view = fakeView();
        const first = marker({ lat: 0, lng: 0 });
        const second = marker({ lat: 50, lng: 50 });
        group.addLayers([first, second]);
        group.addTo(view);
        await settle();
        expect([first.isOnMap(), second.isOnMap()]).toEqual([true, true]);
    });

    test("leaves a marker outside the viewport undrawn", async () => {
        // The whole point of the rebuild: an account with 30,000 pins must not materialise 30,000
        // elements, so a pin the viewport does not contain has no element at all.
        const group = createMaplibreClusterGroup();
        const view = fakeView(6, { south: -1, west: -1, north: 1, east: 1 });
        const inside = marker({ lat: 0, lng: 0 });
        const outside = marker({ lat: 40, lng: 40 });
        group.addLayers([inside, outside]);
        group.addTo(view);
        await settle();
        expect(inside.isOnMap()).toBe(true);
        expect(outside.isOnMap()).toBe(false);
        expect(outside.getElement()).toBeNull();
    });

    test("collapses neighbours into one badge carrying their count", async () => {
        const group = createMaplibreClusterGroup();
        const view = fakeView(2);
        const near = [marker({ lat: 0, lng: 0 }), marker({ lat: 0.001, lng: 0.001 }), marker({ lat: 0.002, lng: 0.002 })];
        group.addLayers(near);
        group.addTo(view);
        await settle();
        expect(badgeCounts(stub)).toEqual([3]);
        expect(near.every((pin) => !pin.isOnMap())).toBe(true);
    });

    test("draws a lone marker as itself rather than a badge of one", async () => {
        const group = createMaplibreClusterGroup();
        const view = fakeView(2);
        const lone = marker({ lat: 0, lng: 0 });
        group.addLayers([lone]);
        group.addTo(view);
        await settle();
        expect(badgeCounts(stub)).toEqual([]);
        expect(lone.isOnMap()).toBe(true);
    });

    test("splits a cluster once the viewport zooms in past it", async () => {
        const group = createMaplibreClusterGroup();
        const view = fakeView(2);
        const near = [marker({ lat: 0, lng: 0 }), marker({ lat: 0.05, lng: 0.05 })];
        group.addLayers(near);
        group.addTo(view);
        await settle();
        expect(badgeCounts(stub)).toEqual([2]);

        view.zoom = 16;
        view.bounds = { south: -1, west: -1, north: 1, east: 1 };
        view.fire("zoomend");
        await settle();
        expect(badgeCounts(stub)).toEqual([]);
        expect(near.every((pin) => pin.isOnMap())).toBe(true);
    });

    test("takes a marker off the map when the viewport moves away from it", async () => {
        const group = createMaplibreClusterGroup();
        const view = fakeView(6, { south: -1, west: -1, north: 1, east: 1 });
        const pin = marker({ lat: 0, lng: 0 });
        group.addLayers([pin]);
        group.addTo(view);
        await settle();
        expect(pin.isOnMap()).toBe(true);

        view.bounds = { south: 40, west: 40, north: 42, east: 42 };
        view.fire("moveend");
        await settle();
        expect(pin.isOnMap()).toBe(false);
    });

    test("coalesces a synchronous batch into a single rebuild", async () => {
        const group = createMaplibreClusterGroup();
        const view = fakeView(2);
        group.addTo(view);
        await settle();
        let regroups = 0;
        group.on("regroup", () => (regroups += 1));
        group.addLayers([marker({ lat: 0, lng: 0 })]);
        group.addLayers([marker({ lat: 20, lng: 20 })]);
        group.addLayer(marker({ lat: 40, lng: 40 }));
        await settle();
        expect(regroups).toBe(1);
    });

    test("announces a regroup so the page can restamp its selection classes", async () => {
        const group = createMaplibreClusterGroup();
        const view = fakeView(2);
        let regroups = 0;
        group.on("regroup", () => (regroups += 1));
        group.addLayers([marker({ lat: 0, lng: 0 })]);
        group.addTo(view);
        await settle();
        expect(regroups).toBeGreaterThan(0);
    });

    test("zooms to where a cluster splits when its badge is clicked", async () => {
        const group = createMaplibreClusterGroup();
        const view = fakeView(2);
        group.addLayers([marker({ lat: 0, lng: 0 }), marker({ lat: 0.05, lng: 0.05 })]);
        group.addTo(view);
        await settle();
        const badge = stub.markers.find((entry) => entry.onMap && entry.element.querySelector(".pin-cluster"));
        badge!.element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect(view.setViewCalls).toHaveLength(1);
        expect(view.setViewCalls[0]!.zoom).toBeGreaterThan(2);
    });

    test("never zooms past the map's own maximum when a badge is clicked", async () => {
        // Coincident points never split, so supercluster answers past its own cluster ceiling.
        // A map whose maximum is lower than that is the only case that tells the clamp apart from
        // no clamp at all - on a map that happens to allow 21 either answer looks right.
        const group = createMaplibreClusterGroup();
        const view = fakeView(2, undefined, 18);
        group.addLayers([marker({ lat: 0, lng: 0 }), marker({ lat: 0, lng: 0 })]);
        group.addTo(view);
        await settle();
        const badge = stub.markers.find((entry) => entry.onMap && entry.element.querySelector(".pin-cluster"));
        badge!.element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect(view.setViewCalls[0]!.zoom).toBe(18);
    });

    test("removes a drawn marker the moment it leaves the group", async () => {
        const group = createMaplibreClusterGroup();
        const view = fakeView(2);
        const pin = marker({ lat: 0, lng: 0 });
        group.addLayers([pin]);
        group.addTo(view);
        await settle();
        group.removeLayer(pin);
        expect(pin.isOnMap()).toBe(false);
        expect(group.hasLayer(pin)).toBe(false);
    });

    test("clears every drawn marker and badge", async () => {
        const group = createMaplibreClusterGroup();
        const view = fakeView(2);
        const pins = [marker({ lat: 0, lng: 0 }), marker({ lat: 0.001, lng: 0.001 }), marker({ lat: 40, lng: 40 })];
        group.addLayers(pins);
        group.addTo(view);
        await settle();
        group.clearLayers();
        await settle();
        expect(group.getLayers()).toEqual([]);
        expect(badgeCounts(stub)).toEqual([]);
        expect(pins.every((pin) => !pin.isOnMap())).toBe(true);
    });

    test("re-evaluates the radius policy as the zoom changes", async () => {
        const asked: number[] = [];
        const group = createMaplibreClusterGroup({ radiusForZoom: (zoom) => (asked.push(zoom), zoom <= 10 ? 60 : 10) });
        const view = fakeView(2);
        group.addLayers([marker({ lat: 0, lng: 0 })]);
        group.addTo(view);
        await settle();
        view.zoom = 15;
        view.fire("zoomend");
        await settle();
        expect(asked).toContain(2);
        expect(asked).toContain(15);
    });

    test("stops listening to the map once removed", async () => {
        const group = createMaplibreClusterGroup();
        const view = fakeView(2);
        const pin = marker({ lat: 0, lng: 0 });
        group.addLayers([pin]);
        group.addTo(view);
        await settle();
        group.remove();
        expect(view.listenerCount()).toBe(0);
        expect(pin.isOnMap()).toBe(false);
    });

    test("holds markers added before it was ever put on a map", () => {
        const group = createMaplibreClusterGroup();
        const pin = marker({ lat: 0, lng: 0 });
        group.addLayer(pin);
        expect(group.getLayers()).toEqual([pin]);
        expect(pin.isOnMap()).toBe(false);
    });
});
