/**
 * createTemporalImagerySlider() itself is Leaflet/DOM-coupled (it drives a
 * real map and reads elements out of a container), so - mirroring
 * map-layers.test.ts's scope - only the pure helpers factored out of it are
 * unit tested here: label formatting, URL-template substitution, and the
 * slider's min/max bounds.
 */
import { describe, expect, test } from "bun:test";
import { formatYearLabel, sliderRange, temporalFeaturesUrl } from "./temporal-imagery";

describe("formatYearLabel", () => {
    test("shows the plain year below the max", () => {
        expect(formatYearLabel(1950, 2026)).toBe("1950");
        expect(formatYearLabel(1900, 2026)).toBe("1900");
    });

    test("shows \"Today\" at (or past) the max", () => {
        expect(formatYearLabel(2026, 2026)).toBe("Today");
        expect(formatYearLabel(2027, 2026)).toBe("Today");
    });
});

describe("temporalFeaturesUrl", () => {
    test("substitutes the 9999 placeholder with the chosen year", () => {
        expect(temporalFeaturesUrl("/dashboard/map/pin/some-pin/temporal/9999/", 1950)).toBe("/dashboard/map/pin/some-pin/temporal/1950/");
    });

    test("substitutes only the trailing placeholder, not a coincidental 9999 earlier in the URL (e.g. inside a slug)", () => {
        expect(temporalFeaturesUrl("/dashboard/wiki/place-9999-annex/temporal/9999/", 1999)).toBe("/dashboard/wiki/place-9999-annex/temporal/1999/");
    });
});

describe("sliderRange", () => {
    test("spans from the earliest covered year through the current year", () => {
        expect(sliderRange([1950, 1980, 2010], 2026)).toEqual({ min: 1950, max: 2026 });
    });

    test("does not assume the years list is sorted", () => {
        expect(sliderRange([2010, 1950, 1980], 2026)).toEqual({ min: 1950, max: 2026 });
    });

    test("clamps min to the current year if coverage somehow reports a future year", () => {
        expect(sliderRange([2030], 2026)).toEqual({ min: 2026, max: 2026 });
    });
});
