import { afterEach, describe, expect, test } from "bun:test";
import { buildRasterStyle, fromOwnTileProtocolUrl, toMapLibreTileUrls } from "./maplibre-raster-style";

const realMaplibregl = (globalThis as Record<string, unknown>).maplibregl;
const realFetch = globalThis.fetch;

afterEach(() => {
    (globalThis as Record<string, unknown>).maplibregl = realMaplibregl;
    globalThis.fetch = realFetch;
});

/**
 * MapLibre loads a raster tile through its image pipeline, which offers no way to queue a request
 * or ask again - `transformRequest` is synchronous. A registered protocol handler is the documented
 * way in, so this deployment's own tiles are handed to MapLibre under one, and get the same pacing
 * and retry as the Leaflet and export paths. Without it a refused tile is permanent: MapLibre marks
 * it `errored`, and its own `reload()` skips errored tiles.
 */
describe("this deployment's own tiles under MapLibre", () => {
    function stubMaplibre(): { protocols: Record<string, (params: { url: string }, controller: AbortController) => Promise<{ data: ArrayBuffer }>> } {
        const state = { protocols: {} as Record<string, (params: { url: string }, controller: AbortController) => Promise<{ data: ArrayBuffer }>> };
        (globalThis as Record<string, unknown>).maplibregl = {
            addProtocol: (name: string, load: (params: { url: string }, controller: AbortController) => Promise<{ data: ArrayBuffer }>) => {
                state.protocols[name] = load;
            },
        };
        return state;
    }

    test("a proxy template is handed over under a protocol MapLibre will hand back", () => {
        stubMaplibre();

        expect(toMapLibreTileUrls("/dashboard/map/basemap-tiles/street/{z}/{x}/{y}/")).toEqual([
            "ultile:///dashboard/map/basemap-tiles/street/{z}/{x}/{y}/",
        ]);
    });

    test("the handler is registered, and resolves back to the real path", async () => {
        const maplibre = stubMaplibre();
        const requested: string[] = [];
        globalThis.fetch = ((url: string) => {
            requested.push(String(url));
            return Promise.resolve({ ok: true, status: 200, arrayBuffer: () => Promise.resolve(new ArrayBuffer(4)) } as Response);
        }) as unknown as typeof fetch;

        const [url] = toMapLibreTileUrls("/dashboard/map/basemap-tiles/street/{z}/{x}/{y}/");
        const handler = maplibre.protocols.ultile;
        expect(handler).toBeDefined();

        const result = await handler!({ url: url!.replace("{z}/{x}/{y}", "3/1/2") }, new AbortController());

        expect(requested).toEqual(["/dashboard/map/basemap-tiles/street/3/1/2/"]);
        expect(result.data.byteLength).toBe(4);
    });

    test("a vendor's URL is left on https, so nothing routes a CDN through the proxy's queue", () => {
        stubMaplibre();

        for (const url of toMapLibreTileUrls("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png")) {
            expect(url.startsWith("https://")).toBe(true);
        }
    });

    test("a URL that never went through the rewrite is returned as-is", () => {
        expect(fromOwnTileProtocolUrl("https://example.test/3/1/2.png")).toBe("https://example.test/3/1/2.png");
    });

    /** Map pages load `maplibregl` from a CDN; pages with no map do not, and this module is on both. */
    test("a page without MapLibre still gets a URL rather than a crash", () => {
        delete (globalThis as Record<string, unknown>).maplibregl;

        expect(() => toMapLibreTileUrls("/dashboard/map/basemap-tiles/street/{z}/{x}/{y}/")).not.toThrow();
    });
});

describe("toMapLibreTileUrls", () => {
    test("expands {s} into one URL per Leaflet's default a/b/c subdomains", () => {
        expect(toMapLibreTileUrls("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png")).toEqual([
            "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
            "https://b.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
            "https://c.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
        ]);
    });

    test("a template with no {s} produces exactly one URL, unchanged apart from {r}", () => {
        expect(toMapLibreTileUrls("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}")).toEqual([
            "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        ]);
    });

    test("{r} is dropped, not translated to MapLibre's {ratio} - this app never sets detectRetina", () => {
        for (const url of toMapLibreTileUrls("https://{s}.tile.example.com/{z}/{x}/{y}{r}.png")) {
            expect(url).not.toContain("{r}");
            expect(url).not.toContain("{ratio}");
        }
    });

    test("{z}/{x}/{y} tokens are left untouched for MapLibre to substitute itself", () => {
        for (const url of toMapLibreTileUrls("https://{s}.tile.example.com/{z}/{x}/{y}.png")) {
            expect(url).toContain("{z}");
            expect(url).toContain("{x}");
            expect(url).toContain("{y}");
        }
    });

    test("a custom subdomains array overrides Leaflet's a/b/c default", () => {
        expect(toMapLibreTileUrls("https://{s}.tile.example.com/{z}/{x}/{y}.png", ["1", "2", "3", "4"])).toEqual([
            "https://1.tile.example.com/{z}/{x}/{y}.png",
            "https://2.tile.example.com/{z}/{x}/{y}.png",
            "https://3.tile.example.com/{z}/{x}/{y}.png",
            "https://4.tile.example.com/{z}/{x}/{y}.png",
        ]);
    });

    test("a custom subdomains string (Leaflet's own shorthand) is split per-character, same as Leaflet itself", () => {
        expect(toMapLibreTileUrls("https://{s}.tile.example.com/{z}/{x}/{y}.png", "xyz")).toEqual([
            "https://x.tile.example.com/{z}/{x}/{y}.png",
            "https://y.tile.example.com/{z}/{x}/{y}.png",
            "https://z.tile.example.com/{z}/{x}/{y}.png",
        ]);
    });

    test("an explicit empty subdomains array is honored as given, not silently replaced by the a/b/c default", () => {
        expect(toMapLibreTileUrls("https://{s}.tile.example.com/{z}/{x}/{y}.png", [])).toEqual([]);
    });
});

describe("buildRasterStyle", () => {
    test("produces a version-8 style with exactly one source and one layer", () => {
        const style = buildRasterStyle("street", { url: "https://{s}.example.com/{z}/{x}/{y}.png", attribution: "Example" });
        expect(style.version).toBe(8);
        expect(Object.keys(style.sources)).toEqual(["street"]);
        expect(style.layers).toEqual([{ id: "street", type: "raster", source: "street" }]);
    });

    test("the source is tagged raster, 256px tiles, with the given attribution/zoom bounds", () => {
        const style = buildRasterStyle("topographic", {
            url: "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
            attribution: "OpenTopoMap contributors",
            minZoom: 0,
            maxNativeZoom: 17,
        });
        const source = style.sources.topographic;
        expect(source?.type).toBe("raster");
        expect(source?.tileSize).toBe(256);
        expect(source?.attribution).toBe("OpenTopoMap contributors");
        expect(source?.minzoom).toBe(0);
        expect(source?.maxzoom).toBe(17);
    });

    test("an id with no {s} in its URL still resolves through the same path (Esri-shaped vendors)", () => {
        const style = buildRasterStyle("satellite", {
            url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
            attribution: "Esri",
        });
        expect(style.sources.satellite?.tiles).toHaveLength(1);
    });

    test("omitted optional fields are omitted, not written as null/undefined placeholders", () => {
        const style = buildRasterStyle("x", { url: "https://tile.example.com/{z}/{x}/{y}.png" });
        const source = style.sources.x;
        expect(source?.attribution).toBeUndefined();
        expect(source?.minzoom).toBeUndefined();
        expect(source?.maxzoom).toBeUndefined();
    });

    test("a source's custom subdomains reach the built tiles array, not just Leaflet's a/b/c default", () => {
        const style = buildRasterStyle("custom", {
            url: "https://{s}.tile.example.com/{z}/{x}/{y}.png",
            subdomains: ["01", "02"],
        });
        expect(style.sources.custom?.tiles).toEqual([
            "https://01.tile.example.com/{z}/{x}/{y}.png",
            "https://02.tile.example.com/{z}/{x}/{y}.png",
        ]);
    });
});
