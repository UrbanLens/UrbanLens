/**
 * normalizeBase() mirrors LEGACY_LAYER_MODE_ALIASES in dashboard/models/markup/meta.py.
 */
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { BASE_ERROR_TILE_COLOR, createMapLayers, normalizeBase, rasterSourceFor, registerRedataLayers, resetRedataLayersCacheForTests, tileLayer, vectorStyleFor } from "./map-layers";
import { acquireOwnTileSlot, ownTileRetriesAreSuspended, recordOwnTileOutcome, resetOwnTileGateForTests } from "./own-tiles";

describe("normalizeBase", () => {
    test("passes canonical keys through unchanged", () => {
        expect(normalizeBase("street")).toBe("street");
        expect(normalizeBase("topographic")).toBe("topographic");
        expect(normalizeBase("satellite")).toBe("satellite");
    });

    test("maps legacy aliases to their canonical key", () => {
        expect(normalizeBase("standard")).toBe("street");
        expect(normalizeBase("osm")).toBe("street");
        expect(normalizeBase("topo")).toBe("topographic");
        expect(normalizeBase("terrain")).toBe("topographic");
    });

    test("is case-insensitive", () => {
        expect(normalizeBase("STREET")).toBe("street");
        expect(normalizeBase("Topo")).toBe("topographic");
        expect(normalizeBase("Satellite")).toBe("satellite");
    });

    test("falls back to street for unknown or missing values", () => {
        expect(normalizeBase("nonsense")).toBe("street");
        expect(normalizeBase("")).toBe("street");
        expect(normalizeBase(null)).toBe("street");
        expect(normalizeBase(undefined)).toBe("street");
    });
});

const realL = (globalThis as Record<string, unknown>).L;

interface StubTileLayer {
    __kind: string;
    url: string;
    options: Record<string, unknown>;
    /** Handlers `tileLayer()` attached, by event name - how the retry wiring is observed. */
    handlers: Record<string, Array<(event: unknown) => void>>;
    on(type: string, fn: (event: unknown) => void): StubTileLayer;
    /** Counted, not just answered: rebuilding a tile URL after the fact is the bug, not the feature. */
    getTileUrlCalls: number;
    getTileUrl(coords: { x: number; y: number; z: number }): string;
}

interface LeafletStub {
    calls: Array<{ url: string; options: Record<string, unknown> }>;
    layers: StubTileLayer[];
    /** The `createTile` Leaflet would have subclassed, for the tiles this deployment serves itself. */
    createTile: ((this: StubTileLayer, coords: { x: number; y: number; z: number }, done: (error?: Error, tile?: HTMLElement) => void) => HTMLElement) | null;
}

function stubLeaflet(): LeafletStub {
    const state: LeafletStub = { calls: [], layers: [], createTile: null };
    const makeLayer = (url: string, options: Record<string, unknown>): StubTileLayer => {
        state.calls.push({ url, options });
        const layer: StubTileLayer = {
            __kind: "tileLayer",
            url,
            options,
            handlers: {},
            on(type, fn) {
                (this.handlers[type] ??= []).push(fn);
                return this;
            },
            getTileUrlCalls: 0,
            getTileUrl(coords) {
                this.getTileUrlCalls++;
                return url.replace("{z}", String(coords.z)).replace("{x}", String(coords.x)).replace("{y}", String(coords.y));
            },
        };
        state.layers.push(layer);
        return layer;
    };
    (globalThis as Record<string, unknown>).L = {
        tileLayer: makeLayer,
        TileLayer: {
            // Mirrors Leaflet's own Class.extend: the prototype is captured, and constructing the
            // result yields a layer carrying it.
            extend: (proto: { createTile: LeafletStub["createTile"] }) => {
                state.createTile = proto.createTile;
                return function (this: unknown, url: string, options: Record<string, unknown>) {
                    return makeLayer(url, options);
                };
            },
        },
    };
    return state;
}

afterEach(() => {
    (globalThis as Record<string, unknown>).L = realL;
});

/**
 * OSM's own tile servers enforce a usage policy (osm.wiki/Blocked) against
 * unauthorized production hotlinking - a burst of requests on zoom-out (a new
 * zoom level's worth of tiles, all uncached) trips it and OSM answers with a
 * rendered "Access blocked" warning tile, at a 200 status, so it isn't caught
 * by errorTileUrl below (that only fires on an actual load failure). Every
 * built-in base layer must instead go through a vendor whose terms permit
 * this, the way "dark" already uses CARTO instead of OSM's own servers.
 */
describe("built-in tile sources do not hotlink OSM's own policy-enforced servers", () => {
    test.each(["street", "dark", "topographic", "satellite"])("%s does not point at tile.openstreetmap.org", (kind) => {
        const state = stubLeaflet();
        tileLayer(kind);
        expect(state.calls[0]?.url).not.toContain("tile.openstreetmap.org");
    });
});

/**
 * A failed base tile (a transient 403/5xx from the vendor CDN, most often
 * seen bursting on zoom-out) must render as a neutral placeholder rather
 * than a browser broken-image icon or the vendor's own error graphic.
 */
describe("tileLayer errorTileUrl", () => {
    test.each(["street", "dark", "topographic", "satellite"])("%s base layer gets an opaque grey placeholder", (kind) => {
        const state = stubLeaflet();
        tileLayer(kind);
        expect(state.calls).toHaveLength(1);
        const errorTileUrl = state.calls[0]?.options.errorTileUrl as string;
        expect(errorTileUrl).toContain("data:image/svg+xml");
        expect(errorTileUrl).not.toBe("");
    });

    test("the borders overlay gets a transparent placeholder, not the opaque base one", () => {
        const state = stubLeaflet();
        tileLayer("borders");
        const errorTileUrl = state.calls[0]?.options.errorTileUrl as string;
        expect(errorTileUrl).toContain("data:image/gif");
    });

    test("legacy base aliases still resolve to a placeholder", () => {
        const state = stubLeaflet();
        tileLayer("topo");
        expect(state.calls[0]?.options.errorTileUrl).toBeTruthy();
    });

    test("an unknown kind falls back to street's placeholder", () => {
        const state = stubLeaflet();
        tileLayer("nonsense");
        const streetState = stubLeaflet();
        tileLayer("street");
        expect(state.calls[0]?.options.errorTileUrl).toBe(streetState.calls[0]?.options.errorTileUrl);
    });

    test("extraOptions can override the placeholder without losing the rest of the defaults", () => {
        const state = stubLeaflet();
        tileLayer("street", { errorTileUrl: "custom.png" });
        expect(state.calls[0]?.options.errorTileUrl).toBe("custom.png");
        expect(state.calls[0]?.options.maxZoom).toBe(21);
    });

    /** The MapLibre engine's background layer (`maplibre-layers.ts`) paints this same exported colour, so the two placeholders cannot drift apart. */
    test("the base placeholder's fill is built from the exported BASE_ERROR_TILE_COLOR", () => {
        const state = stubLeaflet();
        tileLayer("street");
        const errorTileUrl = state.calls[0]?.options.errorTileUrl as string;
        expect(errorTileUrl).toContain(`fill='${encodeURIComponent(BASE_ERROR_TILE_COLOR)}'`);
    });
});

describe("rasterSourceFor opacity", () => {
    test("carries the borders overlay's opacity through for the MapLibre engine to draw with", () => {
        expect(rasterSourceFor("borders").opacity).toBe(0.6);
    });

    test("omits opacity for a base layer TILE_DEFS gives none, so callers default to opaque", () => {
        expect(rasterSourceFor("street").opacity).toBeUndefined();
    });
});

describe("registerRedataLayers", () => {
    const realFetch = globalThis.fetch;

    function stubFetch(response: { ok?: boolean; body?: unknown } | "reject"): { calls: string[] } {
        const state = { calls: [] as string[] };
        globalThis.fetch = ((url: string) => {
            state.calls.push(String(url));
            if (response === "reject") return Promise.reject(new Error("network error"));
            return Promise.resolve({
                ok: response.ok ?? true,
                json: () => Promise.resolve(response.body),
            } as Response);
        }) as unknown as typeof fetch;
        return state;
    }

    beforeEach(() => {
        resetRedataLayersCacheForTests();
    });

    afterEach(() => {
        globalThis.fetch = realFetch;
        resetRedataLayersCacheForTests();
    });

    test("registers a raster layer's url_template under its own id", async () => {
        stubFetch({
            body: {
                layers: [{ id: "custom", source_type: "raster", url_template: "https://x/{z}/{x}/{y}.png", attribution: "Attr", min_zoom: 1, max_zoom: 18 }],
            },
        });
        expect(await registerRedataLayers()).toEqual(["custom"]);

        const state = stubLeaflet();
        tileLayer("custom");
        expect(state.calls[0]?.url).toBe("https://x/{z}/{x}/{y}.png");
        expect(state.calls[0]?.options.attribution).toBe("Attr");
    });

    test("aliases REData's terrain id to this site's topographic key", async () => {
        stubFetch({
            body: { layers: [{ id: "terrain", source_type: "raster", url_template: "https://terrain/{z}/{x}/{y}.png", attribution: "Attr" }] },
        });
        expect(await registerRedataLayers()).toEqual(["topographic"]);

        // tileLayer("topographic") - not tileLayer("terrain") - is what createMapLayers() actually
        // calls, so the registered override must land under that key to ever take effect.
        const state = stubLeaflet();
        tileLayer("topographic");
        expect(state.calls[0]?.url).toBe("https://terrain/{z}/{x}/{y}.png");
    });

    /**
     * `TILE_DEFS` is module-global and a registration overwrites a built-in entry in place, so a
     * test that registers one leaks it into every later test in the same process unless the reset
     * puts it back. Found exactly that way: an unrelated MapLibre-engine assertion about
     * the topographic base's native depth started reading a registered override's depth instead.
     */
    test("resetting restores a built-in source a registration overwrote", async () => {
        stubFetch({
            body: { layers: [{ id: "terrain", source_type: "raster", url_template: "https://terrain/{z}/{x}/{y}.png", attribution: "Attr", max_zoom: 19 }] },
        });
        await registerRedataLayers();
        const overridden = stubLeaflet();
        tileLayer("topographic");
        expect(overridden.calls[0]?.url).toBe("https://terrain/{z}/{x}/{y}.png");

        resetRedataLayersCacheForTests();

        const restored = stubLeaflet();
        tileLayer("topographic");
        expect(restored.calls[0]?.url).toContain("World_Topo_Map");
        expect(restored.calls[0]?.options.maxNativeZoom).toBe(19);
    });

    test("resetting drops a source that had no built-in entry to restore", async () => {
        stubFetch({
            body: { layers: [{ id: "custom", source_type: "raster", url_template: "https://x/{z}/{x}/{y}.png", attribution: "Attr" }] },
        });
        await registerRedataLayers();

        resetRedataLayersCacheForTests();

        // An unknown key falls back to street, so the registered entry is really gone.
        const state = stubLeaflet();
        tileLayer("custom");
        expect(state.calls[0]?.url).toContain("cartocdn.com");
    });

    test("registers a vector entry as a style document, leaving the raster source for Leaflet", async () => {
        stubFetch({
            body: {
                layers: [{ id: "street", source_type: "vector", style_url: "https://x/style.json", attribution: "Attr", min_zoom: 0, max_zoom: 15 }],
            },
        });
        expect(await registerRedataLayers()).toEqual(["street"]);
        expect(vectorStyleFor("street")).toEqual({ styleUrl: "https://x/style.json", attribution: "Attr", minZoom: 0, maxZoom: 15 });

        // Leaflet has no vector renderer, so "street" must still resolve to a raster template there
        // rather than silently producing a map with no tiles at all.
        const state = stubLeaflet();
        tileLayer("street");
        expect(state.calls[0]?.url).toContain("cartocdn.com");
    });

    test("resolves a vector entry through the same legacy aliases as a raster one", async () => {
        stubFetch({
            body: { layers: [{ id: "terrain", source_type: "vector", style_url: "https://x/terrain.json", attribution: "Attr" }] },
        });
        expect(await registerRedataLayers()).toEqual(["topographic"]);
        expect(vectorStyleFor("topo")?.styleUrl).toBe("https://x/terrain.json");
    });

    test("skips a vector entry with no style_url", async () => {
        stubFetch({ body: { layers: [{ id: "street", source_type: "vector", attribution: "Attr" }] } });
        expect(await registerRedataLayers()).toEqual([]);
        expect(vectorStyleFor("street")).toBeNull();
    });

    test("resetting drops a registered vector source", async () => {
        stubFetch({
            body: { layers: [{ id: "street", source_type: "vector", style_url: "https://x/style.json", attribution: "Attr" }] },
        });
        await registerRedataLayers();

        resetRedataLayersCacheForTests();

        expect(vectorStyleFor("street")).toBeNull();
    });

    test("registers both shapes of a D15 entry, so Leaflet stops falling back to a vendor CDN", async () => {
        stubFetch({
            body: {
                layers: [
                    {
                        id: "street",
                        source_type: "vector",
                        style_url: "https://tiles.urbanlens.org/styles/street.json",
                        url_template: "/dashboard/map/basemap-tiles/street/{z}/{x}/{y}/",
                        attribution: "OSM/Protomaps",
                        min_zoom: 0,
                        max_zoom: 15,
                        fallback_attribution: "Esri",
                        fallback_min_zoom: 0,
                        fallback_max_zoom: 19,
                    },
                ],
            },
        });
        // One entry, registered once, even though it now fills two tables.
        expect(await registerRedataLayers()).toEqual(["street"]);
        expect(vectorStyleFor("street")?.styleUrl).toBe("https://tiles.urbanlens.org/styles/street.json");

        const state = stubLeaflet();
        tileLayer("street");
        expect(state.calls[0]?.url).toBe("/dashboard/map/basemap-tiles/street/{z}/{x}/{y}/");
    });

    test("credits a D15 entry's raster half to the raster half's own source, at its own depth", async () => {
        // The style is Protomaps at z15; the proxied tiles are Esri at z19. Showing one layer's
        // credit over the other's bytes is a licence error, and publishing one's ceiling lets a
        // Leaflet map stop drawing four zoom levels early.
        stubFetch({
            body: {
                layers: [
                    {
                        id: "street",
                        source_type: "vector",
                        style_url: "https://x/street.json",
                        url_template: "/proxy/street/{z}/{x}/{y}/",
                        attribution: "OSM/Protomaps",
                        min_zoom: 0,
                        max_zoom: 15,
                        fallback_attribution: "Esri",
                        fallback_min_zoom: 2,
                        fallback_max_zoom: 19,
                    },
                ],
            },
        });
        await registerRedataLayers();

        expect(vectorStyleFor("street")?.attribution).toBe("OSM/Protomaps");
        expect(vectorStyleFor("street")?.maxZoom).toBe(15);

        const state = stubLeaflet();
        tileLayer("street");
        expect(state.calls[0]?.options.attribution).toBe("Esri");
        expect(state.calls[0]?.options.maxNativeZoom).toBe(19);
        expect(state.calls[0]?.options.minZoom).toBe(2);
    });

    test("falls back to the entry's own attribution and zooms when a D15 entry omits the fallback fields", async () => {
        stubFetch({
            body: {
                layers: [{ id: "street", source_type: "vector", style_url: "https://x/s.json", url_template: "/proxy/street/{z}/{x}/{y}/", attribution: "Only one", min_zoom: 1, max_zoom: 14 }],
            },
        });
        await registerRedataLayers();

        const state = stubLeaflet();
        tileLayer("street");
        expect(state.calls[0]?.options.attribution).toBe("Only one");
        expect(state.calls[0]?.options.maxNativeZoom).toBe(14);
        expect(state.calls[0]?.options.minZoom).toBe(1);
    });

    test("the pre-D15 catalogue's shape leaves Leaflet's default base layer on a vendor CDN", async () => {
        // REData's production catalogue as of 2026-09-20, with `url_template` already rewritten to
        // this deployment's proxy the way `basemap_catalogue.py` hands it to a browser. street and
        // dark went vector-only that day; the other three stayed raster.
        stubFetch({
            body: {
                layers: [
                    { id: "street", source_type: "vector", style_url: "https://tiles.urbanlens.org/styles/street.json", attribution: "OSM/Protomaps", min_zoom: 0, max_zoom: 15 },
                    { id: "dark", source_type: "vector", style_url: "https://tiles.urbanlens.org/styles/dark.json", attribution: "OSM/Protomaps", min_zoom: 0, max_zoom: 15 },
                    { id: "terrain", source_type: "raster", url_template: "/dashboard/map/basemap-tiles/terrain/{z}/{x}/{y}/", attribution: "Attr", min_zoom: 0, max_zoom: 17 },
                    { id: "satellite", source_type: "raster", url_template: "/dashboard/map/basemap-tiles/satellite/{z}/{x}/{y}/", attribution: "Attr", min_zoom: 0, max_zoom: 19 },
                    { id: "borders", source_type: "raster", url_template: "/dashboard/map/basemap-tiles/borders/{z}/{x}/{y}/", attribution: "Attr", min_zoom: 0, max_zoom: 19 },
                ],
            },
        });
        await registerRedataLayers();

        expect(vectorStyleFor("street")?.styleUrl).toBe("https://tiles.urbanlens.org/styles/street.json");
        expect(vectorStyleFor("dark")?.styleUrl).toBe("https://tiles.urbanlens.org/styles/dark.json");

        // A layer REData publishes as vector has no proxied raster left to fall back to, so Leaflet
        // keeps the built-in vendor template: a direct browser-to-vendor fetch rather than this
        // deployment's proxy. `street` is the default base layer, so that is what every Leaflet map
        // draws by default - which is the whole of the main map until it is ported.
        const base = stubLeaflet();
        tileLayer("street");
        expect(base.calls[0]?.url).toContain("cartocdn.com");

        // A layer REData still serves as raster does go through the proxy, so the fallback above is
        // the vector entries' doing rather than the catalogue failing to register at all.
        const raster = stubLeaflet();
        tileLayer("topographic");
        expect(raster.calls[0]?.url).toBe("/dashboard/map/basemap-tiles/terrain/{z}/{x}/{y}/");
    });

    test("treats a missing source_type as raster, matching a REData deployment that predates D11", async () => {
        stubFetch({
            body: { layers: [{ id: "custom", url_template: "https://x/{z}/{x}/{y}.png", attribution: "Attr" }] },
        });
        expect(await registerRedataLayers()).toEqual(["custom"]);
    });

    test("skips a raster entry with no url_template", async () => {
        stubFetch({ body: { layers: [{ id: "custom", source_type: "raster", attribution: "Attr" }] } });
        expect(await registerRedataLayers()).toEqual([]);
    });

    /**
     * The catalogue says where a layer's tiles come from. How this site draws that layer - which
     * pane it sits in, how opaque it is, what a failed tile looks like - is not REData's to
     * change, and REData serves a layer called `borders`, so this is not hypothetical.
     */
    describe("a registered override keeps the layer's own presentation", () => {
        test("borders stays a translucent overlay in the overlay pane", async () => {
            stubFetch({
                body: { layers: [{ id: "borders", source_type: "raster", url_template: "/dashboard/map/basemap-tiles/borders/{z}/{x}/{y}/", attribution: "Attr" }] },
            });
            await registerRedataLayers();

            const state = stubLeaflet();
            tileLayer("borders");
            expect(state.calls[0]?.url).toBe("/dashboard/map/basemap-tiles/borders/{z}/{x}/{y}/");
            expect(state.calls[0]?.options.pane).toBe("overlayPane");
            expect(state.calls[0]?.options.opacity).toBe(0.6);
            // Opaque grey over a base map is the one thing an overlay's failure must not paint.
            expect(state.calls[0]?.options.errorTileUrl).toContain("data:image/gif");
        });

        test("a base layer keeps its grey error placeholder", async () => {
            stubFetch({
                body: { layers: [{ id: "street", source_type: "raster", url_template: "/dashboard/map/basemap-tiles/street/{z}/{x}/{y}/", attribution: "Attr" }] },
            });
            await registerRedataLayers();

            const state = stubLeaflet();
            tileLayer("street");
            expect(state.calls[0]?.options.errorTileUrl).toContain("data:image/svg+xml");
        });

        test("a layer with no built-in counterpart still gets one", async () => {
            stubFetch({
                body: { layers: [{ id: "custom", source_type: "raster", url_template: "https://x/{z}/{x}/{y}.png", attribution: "Attr" }] },
            });
            await registerRedataLayers();

            const state = stubLeaflet();
            tileLayer("custom");
            expect(state.calls[0]?.options.errorTileUrl).toContain("data:image/svg+xml");
        });
    });

    /**
     * The tile proxy refuses a tile with 503 the moment its upstream slots are full, and a first
     * look at an area asks for far more tiles at once than there are slots. Leaflet does not retry
     * a failed tile - it paints `errorTileUrl` and considers the tile finished - so on stock
     * behaviour the proxy's bound does not make a map slow, it puts holes in it. `tileLayer()`
     * gives these layers a `createTile` that queues and asks again instead (see `own-tiles.ts`).
     */
    describe("tiles this deployment serves itself are queued and retried", () => {
        const realSetTimeout = globalThis.setTimeout;
        /** Anything scheduled this far out is the queue's 30s slot watchdog rather than a retry. */
        const WATCHDOG_FLOOR_MS = 20_000;
        let pending: Array<{ fn: () => void; ms: number }>;

        /**
         * Fires the retries that are due, leaving the watchdogs alone - those exist to recover a
         * slot Leaflet abandoned half an hour of tiles ago, and firing them along with a one-second
         * retry hands back slots a test is deliberately holding.
         */
        function runRetries(): void {
            const due = pending.filter((timer) => timer.ms < WATCHDOG_FLOOR_MS);
            pending = pending.filter((timer) => timer.ms >= WATCHDOG_FLOOR_MS);
            due.forEach((timer) => timer.fn());
        }

        beforeEach(() => {
            resetOwnTileGateForTests();
            pending = [];
            globalThis.setTimeout = ((fn: () => void, ms: number) => {
                pending.push({ fn, ms });
                return 0;
            }) as unknown as typeof setTimeout;
        });

        afterEach(() => {
            globalThis.setTimeout = realSetTimeout;
            resetOwnTileGateForTests();
        });

        const COORDS = { x: 1, y: 2, z: 3 };
        const URL = "/dashboard/map/basemap-tiles/street/3/1/2/";
        const PLACEHOLDER = "data:image/gif;base64,placeholder";

        async function proxyLayer(): Promise<LeafletStub> {
            stubFetch({
                body: {
                    layers: [
                        { id: "street", source_type: "raster", url_template: "/dashboard/map/basemap-tiles/street/{z}/{x}/{y}/", attribution: "Attr" },
                    ],
                },
            });
            await registerRedataLayers();
            const state = stubLeaflet();
            tileLayer("street", { errorTileUrl: PLACEHOLDER });
            return state;
        }

        /** Creates one tile the way Leaflet does, attached as its own would be while still in view. */
        async function createTile(state: LeafletStub, done: (error?: Error) => void = () => {}): Promise<HTMLImageElement> {
            const layer = state.layers[0]!;
            const tile = state.createTile!.call(layer, COORDS, done) as HTMLImageElement;
            document.body.appendChild(tile);
            // The slot is taken through a promise, so the request lands a microtask later.
            await Promise.resolve();
            await Promise.resolve();
            return tile;
        }

        test("a tile is requested as soon as a slot is free", async () => {
            const state = await proxyLayer();

            const tile = await createTile(state);

            expect(tile.getAttribute("src")).toBe(URL);
            tile.remove();
        });

        /**
         * The whole point of the queue: the browser asks for a viewport at once, and everything past
         * the proxy's own width has to wait rather than be refused. A tile with no `src` yet has not
         * been requested.
         */
        /**
         * The queue's own arithmetic is `own-tiles.test.ts`'s subject; what matters here is that a
         * tile goes through it at all - that it is not requested until a slot is free, and is as
         * soon as one is. Asserted by holding every slot from outside rather than by making a
         * viewport's worth of tiles, so it does not depend on whether this DOM decides to load them.
         */
        test("a tile is not requested until the queue has room for it", async () => {
            const state = await proxyLayer();
            const held = await Promise.all(Array.from({ length: 6 }, () => acquireOwnTileSlot()));

            const tile = await createTile(state);
            expect(tile.getAttribute("src")).toBeNull();

            held[0]!();
            await Promise.resolve();
            await Promise.resolve();

            expect(tile.getAttribute("src")).toBe(URL);
            held.slice(1).forEach((release) => release());
            tile.remove();
        });

        test("a refused tile is asked for again, at the URL it was asked for the first time", async () => {
            const state = await proxyLayer();
            const tile = await createTile(state);

            tile.onerror?.(new Event("error"));
            // More than the retry may be scheduled - each slot also arms a watchdog - so run them all.
            expect(pending.length).toBeGreaterThan(0);
            tile.removeAttribute("src");
            runRetries();
            await Promise.resolve();
            await Promise.resolve();

            expect(tile.getAttribute("src")).toBe(URL);
            tile.remove();
        });

        /**
         * `getTileUrl()` fills `{z}` from the layer's current zoom rather than from the coords it is
         * given, so resolving it again on a retry paints a tile of somewhere else into this one
         * whenever the user has zoomed in the seconds since it failed.
         */
        test("the URL is resolved once, not again on each retry", async () => {
            const state = await proxyLayer();
            const tile = await createTile(state);

            tile.onerror?.(new Event("error"));
            runRetries();
            await Promise.resolve();

            expect(state.layers[0]?.getTileUrlCalls).toBe(1);
            tile.remove();
        });

        test("retries are bounded, and the tile reports itself finished when they run out", async () => {
            const state = await proxyLayer();
            let reported: Error | undefined | "not yet" = "not yet";
            const tile = await createTile(state, (error) => {
                reported = error;
            });

            for (let attempt = 0; attempt < 12 && reported === "not yet"; attempt++) {
                tile.onerror?.(new Event("error"));
                runRetries();
                await Promise.resolve();
                await Promise.resolve();
            }

            // Leaflet counts outstanding tiles to decide a layer has finished loading, so a tile
            // that gives up silently leaves the map's loading indicator on for good.
            expect(reported).toBeInstanceOf(Error);
            expect(tile.getAttribute("src")).toBe(PLACEHOLDER);
            tile.remove();
        });

        test("giving up hands the slot back, so one dead tile does not narrow the queue", async () => {
            const state = await proxyLayer();
            const dying = await createTile(state);
            for (let attempt = 0; attempt < 6; attempt++) {
                dying.onerror?.(new Event("error"));
                runRetries();
                await Promise.resolve();
                await Promise.resolve();
            }

            const held = [];
            for (let i = 0; i < 6; i++) held.push(await createTile(state));

            expect(held.every((tile) => tile.getAttribute("src") === URL)).toBe(true);
            [dying, ...held].forEach((tile) => tile.remove());
        });

        /**
         * A tile that runs out of attempts while queued is still in line for a slot. Taking it
         * paints this tile over the placeholder Leaflet has already been told about, and keeping it
         * narrows the queue for every tile still trying to draw.
         */
        test("a slot that arrives after the tile gave up is handed back, not drawn", async () => {
            const state = await proxyLayer();
            const held = await Promise.all(Array.from({ length: 6 }, () => acquireOwnTileSlot()));
            let reported: Error | undefined | "not yet" = "not yet";
            const tile = await createTile(state, (error) => {
                reported = error;
            });
            for (let attempt = 0; attempt < 12 && reported === "not yet"; attempt++) {
                tile.onerror?.(new Event("error"));
                runRetries();
                await Promise.resolve();
                await Promise.resolve();
            }
            expect(reported).toBeInstanceOf(Error);

            held.forEach((release) => release());
            await Promise.resolve();
            await Promise.resolve();

            expect(tile.getAttribute("src")).toBe(PLACEHOLDER);
            const wanted = [];
            for (let i = 0; i < 6; i++) wanted.push(await createTile(state));
            expect(wanted.every((waiting) => waiting.getAttribute("src") === URL)).toBe(true);
            [tile, ...wanted].forEach((each) => each.remove());
        });

        /**
         * `errorTileUrl` is a data: URI, so painting it succeeds - and a tile still listening would
         * report the picture of its own failure as a tile the deployment served, clearing the count
         * that stops a whole viewport retrying into an outage, and telling Leaflet twice that one
         * tile had finished.
         */
        test("the error placeholder loading is not a tile the deployment served", async () => {
            const state = await proxyLayer();
            let reports = 0;
            const tile = await createTile(state, () => {
                reports++;
            });
            for (let refusal = 0; refusal < 12; refusal++) recordOwnTileOutcome(false);

            tile.onerror?.(new Event("error"));
            await Promise.resolve();
            // What the browser does once `finish` has pointed the tile at the placeholder.
            tile.dispatchEvent(new Event("load"));
            await Promise.resolve();

            expect(tile.getAttribute("src")).toBe(PLACEHOLDER);
            expect(ownTileRetriesAreSuspended()).toBe(true);
            expect(reports).toBe(1);
            tile.remove();
        });

        test("a vendor's own layer is left alone, since its failures are usually its rate limiter", () => {
            const state = stubLeaflet();
            tileLayer("street");

            expect(state.calls[0]?.url).toContain("cartocdn.com");
            expect(state.createTile).toBeNull();
        });
    });

    /**
     * `{% basemap_tile_catalogue %}` (themes/base.html) writes this element ahead of core.js. It is
     * what keeps a map from drawing a vendor's tiles and swapping afterwards - by which point that
     * vendor has already been handed the coordinates the proxy exists to keep from it.
     */
    describe("the catalogue embedded in the page", () => {
        function embed(layers: unknown[]): void {
            const el = document.createElement("script");
            el.type = "application/json";
            el.id = "ul-basemap-tiles";
            el.textContent = JSON.stringify(layers);
            document.body.appendChild(el);
        }

        afterEach(() => {
            document.getElementById("ul-basemap-tiles")?.remove();
        });

        test("is registered synchronously, before anything can request a tile", () => {
            embed([{ id: "street", source_type: "raster", url_template: "/dashboard/map/basemap-tiles/street/{z}/{x}/{y}/", attribution: "REData" }]);
            const fetched = stubFetch({ body: { layers: [] } });

            // No await anywhere: the very first tileLayer() call already resolves to this
            // deployment's own proxy rather than the built-in vendor.
            const state = stubLeaflet();
            tileLayer("street");

            expect(state.calls[0]?.url).toBe("/dashboard/map/basemap-tiles/street/{z}/{x}/{y}/");
            expect(fetched.calls).toEqual([]);
        });

        test("is authoritative when it offers nothing, so no request is made either", async () => {
            embed([]);
            const fetched = stubFetch({ body: { layers: [{ id: "street", source_type: "raster", url_template: "https://x/{z}/{x}/{y}.png", attribution: "Attr" }] } });

            expect(await registerRedataLayers()).toEqual([]);

            // An empty embed means "this deployment offers no extra layers", which is an answer.
            // Asking again over HTTP would re-introduce the very round trip the embed removes.
            expect(fetched.calls).toEqual([]);
            const state = stubLeaflet();
            tileLayer("street");
            expect(state.calls[0]?.url).toContain("cartocdn.com");
        });

        test("falls back to the fetch when the page carried no embed at all", async () => {
            const fetched = stubFetch({ body: { layers: [{ id: "custom", source_type: "raster", url_template: "https://x/{z}/{x}/{y}.png", attribution: "Attr" }] } });
            expect(await registerRedataLayers()).toEqual(["custom"]);
            expect(fetched.calls).toEqual(["/dashboard/map/basemap-tiles/sources/"]);
        });

        test("survives a malformed embed by falling back to the fetch", async () => {
            const el = document.createElement("script");
            el.type = "application/json";
            el.id = "ul-basemap-tiles";
            el.textContent = "{not json";
            document.body.appendChild(el);
            const fetched = stubFetch({ body: { layers: [] } });

            expect(await registerRedataLayers()).toEqual([]);
            expect(fetched.calls).toEqual(["/dashboard/map/basemap-tiles/sources/"]);
        });

        test("registers a vector entry from the embed too", () => {
            embed([{ id: "terrain", source_type: "vector", style_url: "https://tiles.example/terrain.json", attribution: "Copernicus", max_zoom: 12 }]);
            expect(vectorStyleFor("topographic")?.styleUrl).toBe("https://tiles.example/terrain.json");
            expect(vectorStyleFor("topographic")?.maxZoom).toBe(12);
        });
    });

    /**
     * The self-hosting contract: this project ships to people running their own instance with no
     * REData configured at all, not only to the hosted deployment. `BasemapTileCatalogueView`
     * answers `{"layers": []}` for an unconfigured deployment (see `test_unconfigured_redata_yields_no_layers`
     * in `test_basemap_tile_proxy.py`) - the same shape as a configured-but-empty catalogue - so
     * this is the one client-side test standing for every self-hosted deployment: every built-in
     * base layer and the borders overlay must keep resolving to their free, keyless vendor (CARTO,
     * OpenTopoMap, Esri) exactly as before REData existed, not silently break or go blank.
     */
    test("every built-in layer still resolves to its free vendor when REData is unconfigured (self-hosting)", async () => {
        stubFetch({ body: { layers: [] } });
        expect(await registerRedataLayers()).toEqual([]);

        for (const kind of ["street", "dark", "topographic", "satellite", "borders"]) {
            const state = stubLeaflet();
            tileLayer(kind);
            expect(state.calls[0]?.url).not.toContain("/dashboard/map/basemap-tiles/");
        }
    });

    test("memoizes - a second call does not issue a second fetch", async () => {
        const state = stubFetch({ body: { layers: [] } });
        await registerRedataLayers();
        await registerRedataLayers();
        expect(state.calls).toHaveLength(1);
    });

    test("returns [] and leaves built-ins untouched when the fetch rejects", async () => {
        stubFetch("reject");
        expect(await registerRedataLayers()).toEqual([]);

        const state = stubLeaflet();
        tileLayer("street");
        expect(state.calls[0]?.url).toContain("cartocdn.com");
    });

    test("returns [] when the response is not ok", async () => {
        stubFetch({ ok: false, body: { layers: [{ id: "custom", url_template: "https://x/{z}/{x}/{y}.png", attribution: "Attr" }] } });
        expect(await registerRedataLayers()).toEqual([]);
    });
});

/**
 * A minimal but behaviorally-real L.Map stand-in: enough of the layer/pane
 * API for createMapLayers to run its real setup and teardown logic against,
 * without pulling in actual Leaflet.
 */
class FakeMap {
    private readonly activeLayers = new Set<unknown>();
    private readonly panes = new Map<string, { style: Record<string, string> }>();
    private readonly handlers = new Map<string, Set<(...args: never[]) => void>>();
    private readonly container = document.createElement("div");

    getPane(name: string) {
        return this.panes.get(name);
    }
    createPane(name: string) {
        const pane = { style: {} as Record<string, string> };
        this.panes.set(name, pane);
        return pane;
    }
    hasLayer(layer: unknown): boolean {
        return this.activeLayers.has(layer);
    }
    addLayer(layer: unknown): void {
        this.activeLayers.add(layer);
    }
    removeLayer(layer: unknown): void {
        this.activeLayers.delete(layer);
    }
    on(events: string, handler: (...args: never[]) => void): void {
        for (const event of events.split(" ")) {
            if (!this.handlers.has(event)) this.handlers.set(event, new Set());
            this.handlers.get(event)!.add(handler);
        }
    }
    off(events: string, handler: (...args: never[]) => void): void {
        for (const event of events.split(" ")) this.handlers.get(event)?.delete(handler);
    }
    getContainer() {
        return this.container;
    }
    listenerCount(event: string): number {
        return this.handlers.get(event)?.size ?? 0;
    }
    /** The tile URL of every layer currently on the map, so a test can say which are drawn. */
    activeUrls(): string[] {
        return [...this.activeLayers].map((layer) => (layer as { url?: string }).url ?? "");
    }
    isDrawing(fragment: string): boolean {
        return this.activeUrls().some((url) => url.includes(fragment));
    }
    /** Invokes every handler registered for `event`, the way real Leaflet's `Evented.fire` would. */
    fire(event: string, data: Record<string, unknown> = {}): void {
        for (const handler of this.handlers.get(event) ?? []) (handler as (arg: unknown) => void)(data);
    }
}

function stubLeafletForMapLayers(): void {
    (globalThis as Record<string, unknown>).L = {
        tileLayer: (url: string) => {
            const layer = { url, addTo: (map: FakeMap) => (map.addLayer(layer), layer) };
            return layer;
        },
        // A same-origin def goes through `own-tiles.ts`'s subclass rather than `L.tileLayer`, so a
        // stub without this breaks the moment a catalogue points a layer at this deployment's proxy.
        TileLayer: {
            extend: () =>
                class {
                    url: string;
                    constructor(url: string) {
                        this.url = url;
                    }
                    addTo(map: FakeMap) {
                        map.addLayer(this);
                        return this;
                    }
                    on() {}
                },
        },
    };
}

function stubMatchMedia(): { addCalls: number; removeCalls: number; fire: () => void } {
    const tracker = { addCalls: 0, removeCalls: 0, fire: () => {} };
    (globalThis as Record<string, unknown>).matchMedia = () => {
        let handler: (() => void) | null = null;
        return {
            matches: false,
            addEventListener: (_event: string, listener: () => void) => {
                tracker.addCalls++;
                handler = listener;
                tracker.fire = () => handler?.();
            },
            removeEventListener: (_event: string, listener: () => void) => {
                if (listener === handler) tracker.removeCalls++;
            },
        };
    };
    return tracker;
}

function makeToggleRoot(): HTMLElement {
    const root = document.createElement("div");
    root.innerHTML = '<button data-layers-toggle></button><div data-layers-menu hidden></div>';
    document.body.appendChild(root);
    return root;
}

const realRAF = window.requestAnimationFrame;
const realCAF = window.cancelAnimationFrame;

/** Tracks scheduled/cancelled frame ids without ever running a real animation frame. */
function stubAnimationFrame(): { pendingCount: () => number; cancelledIds: number[] } {
    let nextId = 1;
    const pending = new Set<number>();
    const cancelledIds: number[] = [];
    (globalThis as Record<string, unknown>).requestAnimationFrame = () => {
        const id = nextId++;
        pending.add(id);
        return id;
    };
    (globalThis as Record<string, unknown>).cancelAnimationFrame = (id: number) => {
        cancelledIds.push(id);
        pending.delete(id);
    };
    return { pendingCount: () => pending.size, cancelledIds };
}

describe("createMapLayers destroy()", () => {
    afterEach(() => {
        (globalThis as Record<string, unknown>).L = realL;
        delete (globalThis as Record<string, unknown>).matchMedia;
        window.requestAnimationFrame = realRAF;
        window.cancelAnimationFrame = realCAF;
        document.body.innerHTML = "";
    });

    test("removes the document click listener that closes the panel", () => {
        stubLeafletForMapLayers();
        const map = new FakeMap();
        const root = makeToggleRoot();
        const layers = createMapLayers(map as unknown as L.Map, { root, contextMenu: false });

        layers.openPanel();
        expect(root.classList.contains("is-open")).toBe(true);
        document.body.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect(root.classList.contains("is-open")).toBe(false);

        layers.destroy();

        layers.openPanel();
        expect(root.classList.contains("is-open")).toBe(true);
        document.body.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        // Before the fix, the leaked listener from a still-earlier instance
        // would still close this - after it, nothing is listening any more.
        expect(root.classList.contains("is-open")).toBe(true);
    });

    test("removes the prefers-color-scheme listener when darkMode is 'system'", () => {
        stubLeafletForMapLayers();
        const tracker = stubMatchMedia();
        const map = new FakeMap();
        const layers = createMapLayers(map as unknown as L.Map, { darkMode: "system", contextMenu: false });

        expect(tracker.addCalls).toBe(1);
        layers.destroy();
        expect(tracker.removeCalls).toBe(1);
    });

    test("does not touch matchMedia at all outside 'system' mode", () => {
        stubLeafletForMapLayers();
        const tracker = stubMatchMedia();
        const map = new FakeMap();
        const layers = createMapLayers(map as unknown as L.Map, { darkMode: "light", contextMenu: false });

        expect(tracker.addCalls).toBe(0);
        layers.destroy();
        expect(tracker.removeCalls).toBe(0);
    });

    test("unhooks its layeradd/layerremove listener from the map", () => {
        stubLeafletForMapLayers();
        const map = new FakeMap();
        const layers = createMapLayers(map as unknown as L.Map, { contextMenu: false });

        expect(map.listenerCount("layeradd")).toBeGreaterThan(0);
        layers.destroy();
        expect(map.listenerCount("layeradd")).toBe(0);
    });

    test("a second create+destroy cycle on the same globals leaves no listeners behind", () => {
        stubLeafletForMapLayers();
        const tracker = stubMatchMedia();
        for (let i = 0; i < 3; i++) {
            const map = new FakeMap();
            const root = makeToggleRoot();
            const layers = createMapLayers(map as unknown as L.Map, { root, darkMode: "system", contextMenu: false });
            layers.destroy();
        }
        expect(tracker.addCalls).toBe(3);
        expect(tracker.removeCalls).toBe(3);
    });

    test("unbinds the shared context menu on destroy", () => {
        stubLeafletForMapLayers();
        const map = new FakeMap();
        const layers = createMapLayers(map as unknown as L.Map);

        expect(map.listenerCount("contextmenu")).toBeGreaterThan(0);
        layers.destroy();
        expect(map.listenerCount("contextmenu")).toBe(0);
    });

    test("does not bind a context menu at all when contextMenu is false", () => {
        stubLeafletForMapLayers();
        const map = new FakeMap();
        const layers = createMapLayers(map as unknown as L.Map, { contextMenu: false });

        expect(map.listenerCount("contextmenu")).toBe(0);
        layers.destroy();
        expect(map.listenerCount("contextmenu")).toBe(0);
    });

    test("stops drawing the street base once an opaque satellite layer covers it", () => {
        // Every street tile fetched under satellite is paid for and then hidden. It costs the
        // deployment itself once those tiles go through the proxy rather than a vendor CDN.
        stubLeafletForMapLayers();
        const map = new FakeMap();
        const layers = createMapLayers(map as unknown as L.Map, { contextMenu: false });

        expect(map.isDrawing("cartocdn.com/light_all")).toBe(true);

        layers.setBase("satellite");
        expect(map.isDrawing("World_Imagery")).toBe(true);
        expect(map.isDrawing("cartocdn.com/light_all")).toBe(false);

        // And comes back, or leaving satellite would leave the map with no base at all.
        layers.setBase("street");
        expect(map.isDrawing("cartocdn.com/light_all")).toBe(true);
    });

    test("keeps the street base under topo, whose pane is filtered rather than opaque", () => {
        stubLeafletForMapLayers();
        const map = new FakeMap();
        const layers = createMapLayers(map as unknown as L.Map, { contextMenu: false });

        layers.setBase("topographic");

        expect(map.isDrawing("World_Topo_Map")).toBe(true);
        expect(map.isDrawing("cartocdn.com/light_all")).toBe(true);
    });

    test("credits the layer actually drawn, not the vendor the built-in def happened to name", async () => {
        // Once the catalogue replaces a def, the hardcoded credit names a vendor whose bytes are no
        // longer on screen - which is a licence error, not a cosmetic one.
        resetRedataLayersCacheForTests();
        globalThis.fetch = (() =>
            Promise.resolve({
                ok: true,
                json: () =>
                    Promise.resolve({
                        layers: [{ id: "street", source_type: "vector", style_url: "https://x/s.json", url_template: "/proxy/street/{z}/{x}/{y}/", attribution: "OSM/Protomaps", fallback_attribution: "Esri World Street Map" }],
                    }),
            } as Response)) as unknown as typeof fetch;
        await registerRedataLayers();

        stubLeafletForMapLayers();
        // Runs the callback rather than queueing it, so the credit is readable in the test.
        (globalThis as Record<string, unknown>).requestAnimationFrame = (cb: FrameRequestCallback) => (cb(0), 1);
        const map = new FakeMap();
        const credits: string[] = [];
        createMapLayers(map as unknown as L.Map, { contextMenu: false, onAttribution: (text) => credits.push(text) });

        map.fire("layeradd");

        expect(credits.at(-1)).toContain("Esri World Street Map");
        expect(credits.at(-1)).not.toContain("CARTO");

        // TILE_DEFS is module state; leaving this registered would follow the other tests around.
        resetRedataLayersCacheForTests();
    });

    test("cancels a pending attribution animation frame and stops scheduling new ones after destroy", () => {
        stubLeafletForMapLayers();
        const raf = stubAnimationFrame();
        const map = new FakeMap();
        const attributions: string[] = [];
        const layers = createMapLayers(map as unknown as L.Map, {
            contextMenu: false,
            onAttribution: (text) => attributions.push(text),
        });

        // A layer change schedules a frame but does not run it synchronously.
        map.fire("layeradd");
        expect(raf.pendingCount()).toBe(1);
        expect(attributions).toHaveLength(0);

        layers.destroy();

        expect(raf.cancelledIds).toHaveLength(1);
        expect(raf.pendingCount()).toBe(0);

        // The listener that would have scheduled another frame is gone too.
        map.fire("layeradd");
        expect(raf.pendingCount()).toBe(0);
    });
});

/**
 * `setAttribution` writes the credit into the footer with `textContent`, so whatever the engine
 * builds is shown literally. `TILE_DEFS` attributions are Leaflet-flavoured HTML - the string
 * Leaflet's own control renders as markup - so reading one straight into the credit line puts
 * `&copy; <a href="...">OpenStreetMap</a>` on the page as visible text.
 */
describe("the attribution line", () => {
    afterEach(() => {
        (globalThis as Record<string, unknown>).L = realL;
        delete (globalThis as Record<string, unknown>).matchMedia;
        document.body.innerHTML = "";
        window.requestAnimationFrame = realRAF;
        resetRedataLayersCacheForTests();
    });

    function creditFor(options: Parameters<typeof createMapLayers>[1] = {}): string {
        stubLeafletForMapLayers();
        (globalThis as Record<string, unknown>).requestAnimationFrame = (cb: FrameRequestCallback) => (cb(0), 1);
        const map = new FakeMap();
        const seen: string[] = [];
        createMapLayers(map as unknown as L.Map, { ...options, contextMenu: false, onAttribution: (text) => seen.push(text) });
        // The credit is rebuilt off layeradd/layerremove, so nothing is reported until a layer moves.
        map.fire("layeradd");
        return seen.at(-1)!;
    }

    test("carries no markup or entities, because the footer shows it as text", () => {
        const text = creditFor();

        expect(text).not.toMatch(/<[a-z/]/i);
        expect(text).not.toMatch(/&[a-z#][a-z0-9]*;/i);
    });

    test("still names the vendors it is crediting", () => {
        const text = creditFor();

        expect(text).toContain("OpenStreetMap");
        expect(text).toContain("CARTO");
        expect(text).toContain("Leaflet");
    });
});
