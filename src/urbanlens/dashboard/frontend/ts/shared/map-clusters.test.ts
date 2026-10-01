/**
 * Cluster radius / badge sizing and the ctrl+click memory that starts
 * multi-select. Leaflet itself is not loaded in these tests - the functions
 * under test are pure.
 */
import { afterEach, describe, expect, test } from "bun:test";

import { AdditiveSelectMemory, canCluster, createPinClusterGroup, detailPinClusterRadius, isAdditiveClick, pinClusterIconParts } from "./map-clusters";

describe("detailPinClusterRadius", () => {
    test("is wide when zoomed out so a campus collapses to a badge", () => {
        expect(detailPinClusterRadius(10)).toBe(40);
        expect(detailPinClusterRadius(14)).toBe(40);
    });

    test("tightens through street-level zooms", () => {
        expect(detailPinClusterRadius(15)).toBe(20);
        expect(detailPinClusterRadius(16)).toBe(20);
        expect(detailPinClusterRadius(17)).toBe(8);
    });

    test("drops to 1px from zoom 18 so neighbouring buildings stay clickable", () => {
        expect(detailPinClusterRadius(18)).toBe(1);
        expect(detailPinClusterRadius(21)).toBe(1);
    });
});

describe("pinClusterIconParts", () => {
    test("picks the small/medium/large badge by count", () => {
        expect(pinClusterIconParts(2).html).toContain("pin-cluster--s");
        expect(pinClusterIconParts(2).size).toBe(34);
        expect(pinClusterIconParts(9).size).toBe(34);
        expect(pinClusterIconParts(10).size).toBe(42);
        expect(pinClusterIconParts(99).size).toBe(42);
        expect(pinClusterIconParts(100).size).toBe(50);
    });

    test("renders the count inside the badge", () => {
        expect(pinClusterIconParts(12).html).toContain(">12<");
    });
});

describe("isAdditiveClick", () => {
    test("is true for ctrl or meta on a Leaflet-shaped event", () => {
        expect(isAdditiveClick({ originalEvent: { ctrlKey: true } })).toBe(true);
        expect(isAdditiveClick({ originalEvent: { metaKey: true } })).toBe(true);
        expect(isAdditiveClick({ originalEvent: { ctrlKey: false, metaKey: false } })).toBe(false);
    });

    test("reads native MouseEvent modifiers when there is no originalEvent", () => {
        expect(isAdditiveClick({ ctrlKey: true })).toBe(true);
        expect(isAdditiveClick({ metaKey: true })).toBe(true);
        expect(isAdditiveClick({})).toBe(false);
    });
});

describe("AdditiveSelectMemory", () => {
    test("a lone modifier-click selects only that id", () => {
        const memory = new AdditiveSelectMemory();
        expect(memory.idsForAdditiveStart("b")).toEqual(["b"]);
    });

    test("plain-click then modifier-click on a different id selects both", () => {
        const memory = new AdditiveSelectMemory();
        memory.remember("a");
        expect(memory.idsForAdditiveStart("b")).toEqual(["a", "b"]);
    });

    test("modifier-clicking the same id again does not duplicate it", () => {
        const memory = new AdditiveSelectMemory();
        memory.remember("a");
        expect(memory.idsForAdditiveStart("a")).toEqual(["a"]);
    });

    test("clear drops the remembered id", () => {
        const memory = new AdditiveSelectMemory();
        memory.remember("a");
        memory.clear();
        expect(memory.idsForAdditiveStart("b")).toEqual(["b"]);
    });
});

/**
 * leaflet.markercluster's onAdd does `throw "Map has no maxZoom specified"` - a bare string, not an Error.
 */
describe("canCluster", () => {
    const realL = (globalThis as Record<string, unknown>).L;

    function stubLeaflet(withPlugin: boolean): { layerGroupCalls: number } {
        const counter = { layerGroupCalls: 0 };
        const stub: Record<string, unknown> = {
            layerGroup: () => {
                counter.layerGroupCalls += 1;
                return { __kind: "layerGroup" };
            },
            divIcon: (opts: unknown) => opts,
        };
        if (withPlugin) stub.markerClusterGroup = (opts: unknown) => ({ __kind: "cluster", opts });
        (globalThis as Record<string, unknown>).L = stub;
        return counter;
    }

    afterEach(() => {
        (globalThis as Record<string, unknown>).L = realL;
    });

    const mapWithMaxZoom = (maxZoom: number) => ({ getMaxZoom: () => maxZoom }) as unknown as Parameters<typeof canCluster>[0];

    test("is false when the map reports an infinite maxZoom", () => {
        stubLeaflet(true);
        expect(canCluster(mapWithMaxZoom(Number.POSITIVE_INFINITY))).toBe(false);
    });

    test("is true for a map that declares a finite maxZoom", () => {
        stubLeaflet(true);
        expect(canCluster(mapWithMaxZoom(21))).toBe(true);
    });

    test("is false when the markercluster plugin never loaded, whatever the zoom", () => {
        stubLeaflet(false);
        expect(canCluster(mapWithMaxZoom(21))).toBe(false);
    });
});

describe("createPinClusterGroup", () => {
    const realL = (globalThis as Record<string, unknown>).L;

    afterEach(() => {
        (globalThis as Record<string, unknown>).L = realL;
    });

    function stub(): { layerGroupCalls: number; clusterCalls: number } {
        const counter = { layerGroupCalls: 0, clusterCalls: 0 };
        (globalThis as Record<string, unknown>).L = {
            layerGroup: () => {
                counter.layerGroupCalls += 1;
                return { __kind: "layerGroup" };
            },
            markerClusterGroup: (opts: unknown) => {
                counter.clusterCalls += 1;
                return { __kind: "cluster", opts };
            },
            divIcon: (opts: unknown) => opts,
        };
        return counter;
    }

    const mapWithMaxZoom = (maxZoom: number) => ({ getMaxZoom: () => maxZoom }) as unknown as Parameters<typeof createPinClusterGroup>[1];

    test("falls back to a plain LayerGroup rather than letting markercluster throw", () => {
        const counter = stub();
        const group = createPinClusterGroup({}, mapWithMaxZoom(Number.POSITIVE_INFINITY));
        expect((group as unknown as { __kind: string }).__kind).toBe("layerGroup");
        expect(counter.clusterCalls).toBe(0);
        expect(counter.layerGroupCalls).toBe(1);
    });

    test("still clusters when the map declares a maxZoom", () => {
        const counter = stub();
        const group = createPinClusterGroup({}, mapWithMaxZoom(21));
        expect((group as unknown as { __kind: string }).__kind).toBe("cluster");
        expect(counter.clusterCalls).toBe(1);
        expect(counter.layerGroupCalls).toBe(0);
    });

    /**
     * P92: the main map (entries/map-page.ts) used to build its own
     * L.markerClusterGroup by hand instead of calling this shared helper, so
     * its badge markup could silently drift from every other map's. Its own
     * chunking options (needed for accounts with thousands of pins, unlike a
     * detail map's handful of children) must still reach the real factory
     * call once it goes through the shared helper instead.
     */
    test("passes caller overrides (e.g. chunking, radius) through to the factory", () => {
        stub();
        const group = createPinClusterGroup({ maxClusterRadius: 60, chunkedLoading: true, chunkSize: 400, chunkInterval: 60 }, mapWithMaxZoom(21));
        const opts = (group as unknown as { opts: Record<string, unknown> }).opts;
        expect(opts.maxClusterRadius).toBe(60);
        expect(opts.chunkedLoading).toBe(true);
        expect(opts.chunkSize).toBe(400);
        expect(opts.chunkInterval).toBe(60);
    });

    test("uses the shared numbered badge as the default iconCreateFunction", () => {
        stub();
        const group = createPinClusterGroup({}, mapWithMaxZoom(21));
        const opts = (group as unknown as { opts: { iconCreateFunction: (c: { getChildCount(): number }) => { html: string } } }).opts;
        const icon = opts.iconCreateFunction({ getChildCount: () => 12 });
        expect(icon.html).toContain("pin-cluster");
        expect(icon.html).toContain(">12<");
    });
});

/**
 * The fallback is what a browser without the markercluster plugin actually draws pins through, so
 * it has to answer the whole surface its callers use - not just the part `L.LayerGroup` happens to
 * share with a cluster group. `map-page.ts` adds pins in batches (`addLayers`) on every viewport
 * load, and drops a trip's child pins the same way (`removeLayers`); a `L.LayerGroup` has neither,
 * so on this path the batch call throws and the map draws no pins at all.
 */
describe("createPinClusterGroup fallback surface", () => {
    const realL = (globalThis as Record<string, unknown>).L;

    afterEach(() => {
        (globalThis as Record<string, unknown>).L = realL;
    });

    interface FakeLayerGroup {
        layers: unknown[];
        addLayer(layer: unknown): FakeLayerGroup;
        removeLayer(layer: unknown): FakeLayerGroup;
        getLayers(): unknown[];
    }

    /** A stand-in with exactly the methods `L.LayerGroup` really has - no more. */
    function stubPluginless(): void {
        const layerGroup = (): FakeLayerGroup => {
            const group: FakeLayerGroup = {
                layers: [],
                addLayer(layer) {
                    group.layers.push(layer);
                    return group;
                },
                removeLayer(layer) {
                    const index = group.layers.indexOf(layer);
                    if (index >= 0) group.layers.splice(index, 1);
                    return group;
                },
                getLayers: () => group.layers,
            };
            return group;
        };
        (globalThis as Record<string, unknown>).L = { layerGroup, divIcon: (opts: unknown) => opts };
    }

    test("answers addLayers, which every batched pin load calls", () => {
        stubPluginless();
        const group = createPinClusterGroup();
        expect(typeof group.addLayers).toBe("function");
    });

    test("answers removeLayers, which dropping a trip's child pins calls", () => {
        stubPluginless();
        const group = createPinClusterGroup();
        expect(typeof group.removeLayers).toBe("function");
    });

    test("actually holds the markers a batch added", () => {
        stubPluginless();
        const group = createPinClusterGroup();
        const markers = [{ id: "a" }, { id: "b" }, { id: "c" }] as unknown as L.Layer[];
        group.addLayers(markers);
        expect(group.getLayers()).toEqual(markers);
    });

    test("drops exactly the markers a batch removed", () => {
        stubPluginless();
        const group = createPinClusterGroup();
        const markers = [{ id: "a" }, { id: "b" }, { id: "c" }] as unknown as L.Layer[];
        group.addLayers(markers);
        group.removeLayers([markers[0]!, markers[2]!]);
        expect(group.getLayers()).toEqual([markers[1]!]);
    });

    test("leaves a real cluster group's batches as single calls", () => {
        // markercluster implements these natively in one pass; re-wrapping them as a loop over
        // addLayer would lose the single repaint that makes a chunked load of thousands bearable.
        const calls: string[] = [];
        (globalThis as Record<string, unknown>).L = {
            layerGroup: () => ({}),
            divIcon: (opts: unknown) => opts,
            markerClusterGroup: () => ({
                addLayers: (layers: unknown[]) => void calls.push(`addLayers:${layers.length}`),
                removeLayers: (layers: unknown[]) => void calls.push(`removeLayers:${layers.length}`),
                addLayer: () => void calls.push("addLayer"),
                removeLayer: () => void calls.push("removeLayer"),
            }),
        };
        const group = createPinClusterGroup({}, { getMaxZoom: () => 21 } as unknown as Parameters<typeof createPinClusterGroup>[1]);
        const markers = [{ id: "a" }, { id: "b" }, { id: "c" }] as unknown as L.Layer[];
        group.addLayers(markers);
        group.removeLayers(markers);
        expect(calls).toEqual(["addLayers:3", "removeLayers:3"]);
    });
});
