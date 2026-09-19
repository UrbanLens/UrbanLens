/**
 * normalizeBase() mirrors LEGACY_LAYER_MODE_ALIASES in dashboard/models/markup/meta.py.
 */
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { createMapLayers, normalizeBase, registerRedataLayers, resetRedataLayersCacheForTests, tileLayer, vectorStyleFor } from "./map-layers";

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

function stubLeaflet(): { calls: Array<{ url: string; options: Record<string, unknown> }>; layers: StubTileLayer[] } {
    const state = { calls: [] as Array<{ url: string; options: Record<string, unknown> }>, layers: [] as StubTileLayer[] };
    (globalThis as Record<string, unknown>).L = {
        tileLayer: (url: string, options: Record<string, unknown>) => {
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
     * OpenTopoMap's native depth started reading a registered override's depth instead.
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
        expect(restored.calls[0]?.url).toContain("opentopomap.org");
        expect(restored.calls[0]?.options.maxNativeZoom).toBe(17);
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
     * a failed tile - it paints `errorTileUrl` and considers the tile finished - so unless
     * something retries, the concurrency bound does not make a map slow, it puts holes in it.
     */
    describe("tiles this deployment serves itself are retried", () => {
        const realSetTimeout = globalThis.setTimeout;
        let pending: Array<() => void>;

        beforeEach(() => {
            pending = [];
            globalThis.setTimeout = ((fn: () => void) => {
                pending.push(fn);
                return 0;
            }) as unknown as typeof setTimeout;
        });

        afterEach(() => {
            globalThis.setTimeout = realSetTimeout;
        });

        const COORDS = { x: 1, y: 2, z: 3 };
        const PLACEHOLDER = "data:image/svg+xml;charset=UTF-8,placeholder";

        /** A tile element that is in the document, as Leaflet's own would be while still in view. */
        function attachedTile(): HTMLImageElement {
            const tile = document.createElement("img");
            document.body.appendChild(tile);
            return tile;
        }

        async function proxyLayer(): Promise<StubTileLayer> {
            stubFetch({
                body: { layers: [{ id: "street", source_type: "raster", url_template: "/dashboard/map/basemap-tiles/street/{z}/{x}/{y}/", attribution: "Attr" }] },
            });
            await registerRedataLayers();
            const state = stubLeaflet();
            tileLayer("street");
            return state.layers[0]!;
        }

        /**
         * One tile's life as Leaflet runs it: created with its real URL, announced, then - on
         * failure - overwritten with `errorTileUrl` *before* `tileerror` is fired. That ordering is
         * why the retry cannot read the URL back off the element.
         * @returns The URL the tile was asked for, as the DOM resolved it.
         */
        function failOnce(layer: StubTileLayer, tile: HTMLImageElement): string {
            tile.src = "/dashboard/map/basemap-tiles/street/3/1/2/";
            const requested = tile.src;
            layer.handlers.tileloadstart?.[0]?.({ tile, coords: COORDS });
            tile.src = PLACEHOLDER;
            layer.handlers.tileerror?.[0]?.({ tile, coords: COORDS });
            return requested;
        }

        test("a refused tile is asked for again, at the URL it was asked for the first time", async () => {
            const layer = await proxyLayer();
            const tile = attachedTile();

            const requested = failOnce(layer, tile);
            expect(pending).toHaveLength(1);
            pending[0]!();

            expect(tile.src).toBe(requested);
            tile.remove();
        });

        /**
         * `getTileUrl()` fills `{z}` from the layer's current zoom rather than from the coords it is
         * given, so rebuilding the URL at retry time paints a tile of somewhere else into this one
         * whenever the user has zoomed in the seconds since it failed.
         */
        test("the URL is not rebuilt at retry time", async () => {
            const layer = await proxyLayer();
            const tile = attachedTile();

            failOnce(layer, tile);
            pending[0]!();

            expect(layer.getTileUrlCalls).toBe(0);
            tile.remove();
        });

        test("retries are bounded, so a genuinely dead tile stops asking", async () => {
            const layer = await proxyLayer();
            const tile = attachedTile();
            let scheduled = 0;

            // Every retry fails in turn, the way a layer whose upstream is down would.
            for (let attempt = 0; attempt < 12; attempt++) {
                failOnce(layer, tile);
                scheduled += pending.length;
                pending.forEach((fn) => fn());
                pending = [];
            }

            expect(scheduled).toBeGreaterThan(0);
            expect(scheduled).toBeLessThan(12);
            tile.remove();
        });

        test("a tile already pruned from the map is not re-requested", async () => {
            const layer = await proxyLayer();
            const tile = document.createElement("img"); // never attached: panned away while waiting

            failOnce(layer, tile);
            pending[0]!();

            expect(tile.src).toBe(PLACEHOLDER);
        });

        test("a vendor's own layer is left alone, since its failures are usually its rate limiter", () => {
            const state = stubLeaflet();
            tileLayer("street");

            expect(state.calls[0]?.url).toContain("cartocdn.com");
            expect(state.layers[0]?.handlers.tileerror).toBeUndefined();
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
    /** Invokes every handler registered for `event`, the way real Leaflet's `Evented.fire` would. */
    fire(event: string, data: Record<string, unknown> = {}): void {
        for (const handler of this.handlers.get(event) ?? []) (handler as (arg: unknown) => void)(data);
    }
}

function stubLeafletForMapLayers(): void {
    (globalThis as Record<string, unknown>).L = {
        tileLayer: () => {
            const layer = { addTo: (map: FakeMap) => (map.addLayer(layer), layer) };
            return layer;
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
