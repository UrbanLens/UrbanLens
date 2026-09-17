/**
 * normalizeBase() mirrors LEGACY_LAYER_MODE_ALIASES in dashboard/models/markup/meta.py.
 */
import { afterEach, describe, expect, test } from "bun:test";
import { normalizeBase, tileLayer } from "./map-layers";

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

/**
 * A failed base tile (a transient 403/5xx from the vendor CDN, most often
 * seen bursting on zoom-out) must render as a neutral placeholder rather
 * than a browser broken-image icon or the vendor's own error graphic.
 */
describe("tileLayer errorTileUrl", () => {
    const realL = (globalThis as Record<string, unknown>).L;

    function stubLeaflet(): { calls: Array<{ url: string; options: Record<string, unknown> }> } {
        const state = { calls: [] as Array<{ url: string; options: Record<string, unknown> }> };
        (globalThis as Record<string, unknown>).L = {
            tileLayer: (url: string, options: Record<string, unknown>) => {
                state.calls.push({ url, options });
                return { __kind: "tileLayer", url, options };
            },
        };
        return state;
    }

    afterEach(() => {
        (globalThis as Record<string, unknown>).L = realL;
    });

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
