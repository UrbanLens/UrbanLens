/**
 * Cluster radius / badge sizing and the ctrl+click memory that starts
 * multi-select. Leaflet itself is not loaded in these tests - the functions
 * under test are pure.
 */
import { describe, expect, test } from "bun:test";

import { AdditiveSelectMemory, detailPinClusterRadius, isAdditiveClick, pinClusterIconParts } from "./map-clusters";

describe("detailPinClusterRadius", () => {
    test("is wide when zoomed out so a campus collapses to a badge", () => {
        expect(detailPinClusterRadius(10)).toBe(40);
        expect(detailPinClusterRadius(14)).toBe(40);
    });

    test("tightens through street-level zooms", () => {
        expect(detailPinClusterRadius(15)).toBe(20);
        expect(detailPinClusterRadius(16)).toBe(20);
        expect(detailPinClusterRadius(17)).toBe(8);
    });

    test("drops to 1px from zoom 18 so neighbouring buildings stay clickable", () => {
        expect(detailPinClusterRadius(18)).toBe(1);
        expect(detailPinClusterRadius(21)).toBe(1);
    });
});

describe("pinClusterIconParts", () => {
    test("picks the small/medium/large badge by count", () => {
        expect(pinClusterIconParts(2).html).toContain("pin-cluster--s");
        expect(pinClusterIconParts(2).size).toBe(34);
        expect(pinClusterIconParts(9).size).toBe(34);
        expect(pinClusterIconParts(10).size).toBe(42);
        expect(pinClusterIconParts(99).size).toBe(42);
        expect(pinClusterIconParts(100).size).toBe(50);
    });

    test("renders the count inside the badge", () => {
        expect(pinClusterIconParts(12).html).toContain(">12<");
    });
});

describe("isAdditiveClick", () => {
    test("is true for ctrl or meta on a Leaflet-shaped event", () => {
        expect(isAdditiveClick({ originalEvent: { ctrlKey: true } })).toBe(true);
        expect(isAdditiveClick({ originalEvent: { metaKey: true } })).toBe(true);
        expect(isAdditiveClick({ originalEvent: { ctrlKey: false, metaKey: false } })).toBe(false);
    });

    test("reads native MouseEvent modifiers when there is no originalEvent", () => {
        expect(isAdditiveClick({ ctrlKey: true })).toBe(true);
        expect(isAdditiveClick({ metaKey: true })).toBe(true);
        expect(isAdditiveClick({})).toBe(false);
    });
});

describe("AdditiveSelectMemory", () => {
    test("a lone modifier-click selects only that id", () => {
        const memory = new AdditiveSelectMemory();
        expect(memory.idsForAdditiveStart("b")).toEqual(["b"]);
    });

    test("plain-click then modifier-click on a different id selects both", () => {
        const memory = new AdditiveSelectMemory();
        memory.remember("a");
        expect(memory.idsForAdditiveStart("b")).toEqual(["a", "b"]);
    });

    test("modifier-clicking the same id again does not duplicate it", () => {
        const memory = new AdditiveSelectMemory();
        memory.remember("a");
        expect(memory.idsForAdditiveStart("a")).toEqual(["a"]);
    });

    test("clear drops the remembered id", () => {
        const memory = new AdditiveSelectMemory();
        memory.remember("a");
        memory.clear();
        expect(memory.idsForAdditiveStart("b")).toEqual(["b"]);
    });
});
