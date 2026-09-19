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
});
