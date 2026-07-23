/**
 * normalizeBase() mirrors LEGACY_LAYER_MODE_ALIASES in
 * dashboard/models/markup/meta.py - it's the single place old cached
 * MarkupMap snapshots and pre-canonical layer-mode values get normalized
 * before every Leaflet map on the site picks a base layer. It has no Leaflet
 * or DOM dependency, so it's tested directly rather than through createMapLayers().
 */
import { describe, expect, test } from "bun:test";
import { normalizeBase } from "./map-layers";

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
