import { describe, expect, test } from "bun:test";
import { buildRasterStyle, toMapLibreTileUrls } from "./maplibre-raster-style";

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
