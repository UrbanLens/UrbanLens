/**
 * These are the sanitization and geometry primitives every draw-session shape
 * (line/arrow/circle/rect/polygon/text/pin) is built from - safeColor/
 * safeNumber guard against malformed server-stored shape specs before they
 * ever reach a Leaflet style option, and bearing/arrowheadSize drive arrow
 * rendering. All pure, no Leaflet/DOM dependency, so tested directly.
 */
import { describe, expect, test } from "bun:test";
import { arrowheadSize, bearing, safeColor, safeNumber, safeOptionalColor } from "./markup-engine";

describe("safeColor", () => {
    test("passes through a valid 6-digit hex color", () => {
        expect(safeColor("#1a2b3c")).toBe("#1a2b3c");
    });

    test("is case-insensitive on hex digits", () => {
        expect(safeColor("#ABCDEF")).toBe("#ABCDEF");
    });

    test("falls back to the default for non-hex input", () => {
        expect(safeColor("red")).toBe("#e74c3c");
        expect(safeColor("javascript:alert(1)")).toBe("#e74c3c");
    });

    test("falls back to a custom fallback when given", () => {
        expect(safeColor("not-a-color", "#000000")).toBe("#000000");
    });

    test("rejects non-string values, short hex, and 8-digit hex", () => {
        expect(safeColor(null)).toBe("#e74c3c");
        expect(safeColor(undefined)).toBe("#e74c3c");
        expect(safeColor(123456)).toBe("#e74c3c");
        expect(safeColor("#abc")).toBe("#e74c3c");
        expect(safeColor("#aabbccdd")).toBe("#e74c3c");
    });
});

describe("safeOptionalColor", () => {
    test("passes 'none' through untouched", () => {
        expect(safeOptionalColor("none")).toBe("none");
    });

    test("validates real colors the same as safeColor", () => {
        expect(safeOptionalColor("#112233")).toBe("#112233");
        expect(safeOptionalColor("garbage")).toBe("#e74c3c");
    });
});

describe("safeNumber", () => {
    test("clamps within the given range", () => {
        expect(safeNumber(500, 0, 100, 50)).toBe(100);
        expect(safeNumber(-5, 0, 100, 50)).toBe(0);
        expect(safeNumber(42, 0, 100, 50)).toBe(42);
    });

    test("parses numeric strings", () => {
        expect(safeNumber("17", 0, 100, 50)).toBe(17);
    });

    test("falls back to the default on NaN/non-numeric input", () => {
        expect(safeNumber("not-a-number", 0, 100, 50)).toBe(50);
        expect(safeNumber(undefined, 0, 100, 50)).toBe(50);
        expect(safeNumber(null, 0, 100, 50)).toBe(50);
    });
});

describe("bearing", () => {
    test("0 degrees for due north", () => {
        expect(bearing([0, 0], [1, 0])).toBeCloseTo(0, 6);
    });

    test("90 degrees for due east", () => {
        expect(bearing([0, 0], [0, 1])).toBeCloseTo(90, 6);
    });

    test("180 degrees for due south", () => {
        expect(bearing([0, 0], [-1, 0])).toBeCloseTo(180, 6);
    });

    test("-90 degrees for due west", () => {
        expect(bearing([0, 0], [0, -1])).toBeCloseTo(-90, 6);
    });

    test("accepts {lat,lng} object form equivalently to tuples", () => {
        expect(bearing({ lat: 0, lng: 0 }, { lat: 1, lng: 0 })).toBeCloseTo(bearing([0, 0], [1, 0]), 10);
    });
});

describe("arrowheadSize", () => {
    test("uses the largest size at high zoom or when zoom is omitted", () => {
        expect(arrowheadSize()).toBe(28);
        expect(arrowheadSize(null)).toBe(28);
        expect(arrowheadSize(16)).toBe(28);
        expect(arrowheadSize(20)).toBe(28);
    });

    test("steps down through the zoom breakpoints", () => {
        expect(arrowheadSize(15)).toBe(20);
        expect(arrowheadSize(13)).toBe(20);
        expect(arrowheadSize(12)).toBe(14);
        expect(arrowheadSize(10)).toBe(14);
        expect(arrowheadSize(9)).toBe(8);
        expect(arrowheadSize(0)).toBe(8);
    });
});
