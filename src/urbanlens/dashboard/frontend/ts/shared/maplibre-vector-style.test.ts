/**
 * A style document fetched by hand arrives without the two things MapLibre would have done for one
 * it loaded itself - relative URLs resolved against the style's own address, and sprite/glyphs
 * applied to the map - and its ids have to be made not to collide with a page's own layers before
 * it can be merged into a live style. Each of those is a way to produce a map that draws nothing
 * and reports no error, so each is pinned here.
 */

import { afterEach, describe, expect, test } from "bun:test";
import { fetchVectorStyle, isVectorStyleDocument, namespaceVectorStyle, resolveStyleUrl } from "./maplibre-vector-style";
import type { StyleSpecification } from "maplibre-gl";

const STYLE_URL = "https://redata.example/tiles/styles/street/style.json";

const realFetch = globalThis.fetch;

/** A style shaped like the ones vector basemaps actually ship: relative URLs and a sprite/glyphs pair. */
function styleDocument(overrides: Partial<StyleSpecification> = {}): StyleSpecification {
    return {
        version: 8,
        name: "street",
        sources: {
            openmaptiles: { type: "vector", tiles: ["../../data/{z}/{x}/{y}.pbf"], maxzoom: 14 },
        },
        sprite: "../sprites/street",
        glyphs: "../../fonts/{fontstack}/{range}.pbf",
        layers: [
            { id: "background", type: "background", paint: { "background-color": "#eee" } },
            { id: "water", type: "fill", source: "openmaptiles", "source-layer": "water" },
            { id: "place-label", type: "symbol", source: "openmaptiles", "source-layer": "place", layout: { "text-font": ["Noto Sans"] } },
        ],
        ...overrides,
    } as StyleSpecification;
}

describe("resolveStyleUrl", () => {
    test("leaves an absolute URL alone", () => {
        expect(resolveStyleUrl("https://cdn.example/a.pbf", STYLE_URL)).toBe("https://cdn.example/a.pbf");
    });

    test("leaves a protocol-relative URL alone", () => {
        expect(resolveStyleUrl("//cdn.example/a.pbf", STYLE_URL)).toBe("//cdn.example/a.pbf");
    });

    test("leaves this app's own tile protocol alone", () => {
        expect(resolveStyleUrl("ultile:///dashboard/map/basemap-tiles/street/{z}/{x}/{y}/", STYLE_URL)).toBe(
            "ultile:///dashboard/map/basemap-tiles/street/{z}/{x}/{y}/",
        );
    });

    test("resolves a relative path against the style's own address, not the page's", () => {
        expect(resolveStyleUrl("../../data/tiles.json", STYLE_URL)).toBe("https://redata.example/tiles/data/tiles.json");
    });

    test("resolves a root-relative path against the style's origin", () => {
        expect(resolveStyleUrl("/data/tiles.json", STYLE_URL)).toBe("https://redata.example/data/tiles.json");
    });

    test("keeps MapLibre's own tile tokens as braces rather than percent-encoding them", () => {
        // URL() encodes braces, which would ask the tile server for the literal string "%7Bz%7D"
        // on every request - a layer that loads nothing and reports nothing.
        expect(resolveStyleUrl("../data/{z}/{x}/{y}.pbf", STYLE_URL)).toBe("https://redata.example/tiles/styles/data/{z}/{x}/{y}.pbf");
    });

    test("keeps the glyph tokens too", () => {
        expect(resolveStyleUrl("fonts/{fontstack}/{range}.pbf", STYLE_URL)).toBe(
            "https://redata.example/tiles/styles/street/fonts/{fontstack}/{range}.pbf",
        );
    });
});

describe("isVectorStyleDocument", () => {
    test("accepts a v8 document with sources and layers", () => {
        expect(isVectorStyleDocument(styleDocument())).toBe(true);
    });

    test.each([
        ["null", null],
        ["a string", "{}"],
        ["a number", 8],
        ["an HTML error page parsed as JSON", { error: "not found" }],
        ["a document of the wrong version", { version: 7, sources: {}, layers: [] }],
        ["a document with no sources", { version: 8, layers: [] }],
        ["a document whose layers are not a list", { version: 8, sources: {}, layers: {} }],
    ])("rejects %s", (_label, value) => {
        expect(isVectorStyleDocument(value)).toBe(false);
    });
});

describe("namespaceVectorStyle", () => {
    test("prefixes every source and layer id", () => {
        const style = namespaceVectorStyle("ul-vec-", styleDocument(), STYLE_URL);

        expect(Object.keys(style.sources)).toEqual(["ul-vec-openmaptiles"]);
        expect(style.layers.map((layer) => layer.id)).toEqual(["ul-vec-background", "ul-vec-water", "ul-vec-place-label"]);
    });

    test("repoints each layer at the renamed source", () => {
        const style = namespaceVectorStyle("ul-vec-", styleDocument(), STYLE_URL);

        const water = style.layers.find((layer) => layer.id === "ul-vec-water") as { source?: string };
        expect(water.source).toBe("ul-vec-openmaptiles");
    });

    test("keeps the document's own layer order, which is its draw order", () => {
        const style = namespaceVectorStyle("ul-vec-", styleDocument(), STYLE_URL);

        expect(style.layers.map((layer) => layer.id.replace("ul-vec-", ""))).toEqual(["background", "water", "place-label"]);
    });

    test("keeps a background layer, which has no source to repoint", () => {
        const style = namespaceVectorStyle("ul-vec-", styleDocument(), STYLE_URL);

        expect(style.layers[0]).toMatchObject({ id: "ul-vec-background", type: "background" });
        expect("source" in style.layers[0]!).toBe(false);
    });

    test("leaves source-layer alone - it names a layer inside the tile, not a source of the style", () => {
        const style = namespaceVectorStyle("ul-vec-", styleDocument(), STYLE_URL);

        const water = style.layers.find((layer) => layer.id === "ul-vec-water") as { "source-layer"?: string };
        expect(water["source-layer"]).toBe("water");
    });

    test("drops a layer naming a source the document never defined", () => {
        // MapLibre throws on a dangling source reference, which would abort the whole merge and
        // leave the map with half a basemap on it.
        const doc = styleDocument({
            layers: [
                { id: "water", type: "fill", source: "openmaptiles", "source-layer": "water" },
                { id: "orphan", type: "fill", source: "absent", "source-layer": "x" },
            ],
        } as Partial<StyleSpecification>);

        const style = namespaceVectorStyle("ul-vec-", doc, STYLE_URL);

        expect(style.layers.map((layer) => layer.id)).toEqual(["ul-vec-water"]);
    });

    test("resolves a source's relative tile template", () => {
        const style = namespaceVectorStyle("ul-vec-", styleDocument(), STYLE_URL);

        expect((style.sources["ul-vec-openmaptiles"] as { tiles: string[] }).tiles).toEqual([
            "https://redata.example/tiles/data/{z}/{x}/{y}.pbf",
        ]);
    });

    test("resolves a TileJSON source's url", () => {
        const doc = styleDocument({ sources: { omt: { type: "vector", url: "../../data/v3.json" } } } as Partial<StyleSpecification>);

        const style = namespaceVectorStyle("ul-vec-", doc, STYLE_URL);

        expect((style.sources["ul-vec-omt"] as { url: string }).url).toBe("https://redata.example/tiles/data/v3.json");
    });

    test("resolves a GeoJSON source's data URL but leaves inline data alone", () => {
        const inline = { type: "FeatureCollection", features: [] };
        const doc = styleDocument({
            sources: { remote: { type: "geojson", data: "../features.json" }, local: { type: "geojson", data: inline } },
        } as Partial<StyleSpecification>);

        const style = namespaceVectorStyle("ul-vec-", doc, STYLE_URL);

        expect((style.sources["ul-vec-remote"] as { data: string }).data).toBe("https://redata.example/tiles/styles/features.json");
        expect((style.sources["ul-vec-local"] as { data: unknown }).data).toEqual(inline);
    });

    test("resolves glyphs and normalises a single sprite under MapLibre's default id", () => {
        const style = namespaceVectorStyle("ul-vec-", styleDocument(), STYLE_URL);

        expect(style.glyphs).toBe("https://redata.example/tiles/fonts/{fontstack}/{range}.pbf");
        expect(style.sprites).toEqual([{ id: "default", url: "https://redata.example/tiles/styles/sprites/street" }]);
    });

    test("keeps every entry of a multi-sprite style, since icon-image names carry those ids", () => {
        const doc = styleDocument({
            sprite: [
                { id: "default", url: "../sprites/base" },
                { id: "poi", url: "https://cdn.example/poi" },
            ],
        } as Partial<StyleSpecification>);

        const style = namespaceVectorStyle("ul-vec-", doc, STYLE_URL);

        expect(style.sprites).toEqual([
            { id: "default", url: "https://redata.example/tiles/styles/sprites/base" },
            { id: "poi", url: "https://cdn.example/poi" },
        ]);
    });

    test("reports no sprite or glyphs for a style that ships neither", () => {
        const doc = styleDocument({ sprite: undefined, glyphs: undefined } as Partial<StyleSpecification>);

        const style = namespaceVectorStyle("ul-vec-", doc, STYLE_URL);

        expect(style.sprites).toEqual([]);
        expect(style.glyphs).toBeNull();
    });

    test("does not mutate the document it was given", () => {
        const doc = styleDocument();

        namespaceVectorStyle("ul-vec-", doc, STYLE_URL);

        expect(Object.keys(doc.sources)).toEqual(["openmaptiles"]);
        expect((doc.sources.openmaptiles as { tiles: string[] }).tiles).toEqual(["../../data/{z}/{x}/{y}.pbf"]);
        expect(doc.layers[1]).toMatchObject({ id: "water", source: "openmaptiles" });
    });
});

describe("pmtiles sources", () => {
    interface ProtocolStub {
        addProtocol: (scheme: string, handler: unknown) => void;
        registered: string[];
    }

    /** A fresh stand-in per test, so the module's "already registered on this maplibregl" latch resets with it. */
    function stubMaplibre(): ProtocolStub {
        const stub: ProtocolStub = { registered: [], addProtocol: (scheme) => stub.registered.push(scheme) };
        (globalThis as { maplibregl?: unknown }).maplibregl = stub;
        return stub;
    }

    afterEach(() => {
        delete (globalThis as { maplibregl?: unknown }).maplibregl;
    });

    test("registers the protocol for a style whose source names a PMTiles archive", () => {
        const stub = stubMaplibre();
        // The shape this deployment's own street.json actually uses.
        const doc = styleDocument({
            sources: { protomaps: { type: "vector", url: "pmtiles://https://tiles.example/pmtiles/planet.pmtiles" } },
        } as Partial<StyleSpecification>);

        namespaceVectorStyle("ul-vec-", doc, STYLE_URL);

        expect(stub.registered).toEqual(["pmtiles"]);
    });

    test("registers it for the tiles-array form too", () => {
        const stub = stubMaplibre();
        const doc = styleDocument({
            sources: { p: { type: "vector", tiles: ["pmtiles://https://tiles.example/a.pmtiles/{z}/{x}/{y}"] } },
        } as Partial<StyleSpecification>);

        namespaceVectorStyle("ul-vec-", doc, STYLE_URL);

        expect(stub.registered).toEqual(["pmtiles"]);
    });

    test("registers it once, not once per style", () => {
        const stub = stubMaplibre();
        const doc = styleDocument({
            sources: { p: { type: "vector", url: "pmtiles://https://tiles.example/a.pmtiles" } },
        } as Partial<StyleSpecification>);

        namespaceVectorStyle("ul-vec-", doc, STYLE_URL);
        namespaceVectorStyle("ul-vec2-", doc, STYLE_URL);

        expect(stub.registered).toEqual(["pmtiles"]);
    });

    test("leaves the protocol unregistered for a style that names no archive", () => {
        const stub = stubMaplibre();

        namespaceVectorStyle("ul-vec-", styleDocument(), STYLE_URL);

        expect(stub.registered).toEqual([]);
    });

    test("leaves it unregistered for a style that names a tile endpoint instead of an archive", () => {
        const stub = stubMaplibre();
        // REData is switching street.json/dark.json off `pmtiles://` and onto this; the `pmtiles`
        // package becomes removable only if nothing here still reaches for it.
        const doc = styleDocument({
            sources: { protomaps: { type: "vector", tiles: ["https://tiles.urbanlens.org/basemap/{z}/{x}/{y}"], maxzoom: 15 } },
        } as Partial<StyleSpecification>);

        const style = namespaceVectorStyle("ul-vec-", doc, STYLE_URL);

        expect(stub.registered).toEqual([]);
        expect((style.sources["ul-vec-protomaps"] as { tiles: string[] }).tiles).toEqual(["https://tiles.urbanlens.org/basemap/{z}/{x}/{y}"]);
    });

    test("leaves a pmtiles:// URL unresolved - it is a scheme, not a relative path", () => {
        const doc = styleDocument({
            sources: { p: { type: "vector", url: "pmtiles://https://tiles.example/a.pmtiles" } },
        } as Partial<StyleSpecification>);

        const style = namespaceVectorStyle("ul-vec-", doc, STYLE_URL);

        expect((style.sources["ul-vec-p"] as { url: string }).url).toBe("pmtiles://https://tiles.example/a.pmtiles");
    });

    test("does not reach for a maplibregl that is not on the page", () => {
        const doc = styleDocument({
            sources: { p: { type: "vector", url: "pmtiles://https://tiles.example/a.pmtiles" } },
        } as Partial<StyleSpecification>);

        expect(() => namespaceVectorStyle("ul-vec-", doc, STYLE_URL)).not.toThrow();
    });
});

describe("fetchVectorStyle", () => {
    afterEach(() => {
        globalThis.fetch = realFetch;
    });

    function stubFetch(response: { ok?: boolean; body?: unknown } | "reject"): { calls: string[] } {
        const state = { calls: [] as string[] };
        globalThis.fetch = ((url: string) => {
            state.calls.push(String(url));
            if (response === "reject") return Promise.reject(new Error("network error"));
            return Promise.resolve({ ok: response.ok ?? true, json: () => Promise.resolve(response.body) } as Response);
        }) as unknown as typeof fetch;
        return state;
    }

    test("fetches and namespaces the document", async () => {
        const calls = stubFetch({ body: styleDocument() });

        const style = await fetchVectorStyle("ul-vec-", STYLE_URL);

        expect(calls.calls).toEqual([STYLE_URL]);
        expect(style?.layers.map((layer) => layer.id)).toEqual(["ul-vec-background", "ul-vec-water", "ul-vec-place-label"]);
    });

    test("gives up rather than throwing when the style is refused", async () => {
        stubFetch({ ok: false });

        expect(await fetchVectorStyle("ul-vec-", STYLE_URL)).toBeNull();
    });

    test("gives up rather than throwing when the network fails", async () => {
        stubFetch("reject");

        expect(await fetchVectorStyle("ul-vec-", STYLE_URL)).toBeNull();
    });

    test("gives up when the body is not a style document", async () => {
        // A proxy's HTML error page, or a REData version serving something else at this URL.
        stubFetch({ body: { detail: "Not found" } });

        expect(await fetchVectorStyle("ul-vec-", STYLE_URL)).toBeNull();
    });

    test("gives up when the body is not JSON at all", async () => {
        globalThis.fetch = (() =>
            Promise.resolve({ ok: true, json: () => Promise.reject(new SyntaxError("Unexpected token <")) } as unknown as Response)) as unknown as typeof fetch;

        expect(await fetchVectorStyle("ul-vec-", STYLE_URL)).toBeNull();
    });
});
