import { describe, expect, test } from "bun:test";

import { type Pt, interiorPoint, pointInRing, polygonCentroid } from "./coords";

/** A 10x10 square. */
const SQUARE: Pt[] = [
    { x: 0, y: 0 },
    { x: 10, y: 0 },
    { x: 10, y: 10 },
    { x: 0, y: 10 },
];

/** An L: the top-right quadrant is missing, so the centroid falls outside. */
const L_SHAPE: Pt[] = [
    { x: 0, y: 0 },
    { x: 10, y: 0 },
    { x: 10, y: 3 },
    { x: 3, y: 3 },
    { x: 3, y: 10 },
    { x: 0, y: 10 },
];

describe("polygonCentroid", () => {
    test("a square centres in the middle", () => {
        const centre = polygonCentroid(SQUARE);
        expect(centre.x).toBeCloseTo(5, 9);
        expect(centre.y).toBeCloseTo(5, 9);
    });

    test("extra corners along one side do not drag the centre toward them", () => {
        // The vertex average would; the area-weighted centre does not.
        const subdivided: Pt[] = [
            { x: 0, y: 0 },
            { x: 2, y: 0 },
            { x: 4, y: 0 },
            { x: 6, y: 0 },
            { x: 8, y: 0 },
            { x: 10, y: 0 },
            { x: 10, y: 10 },
            { x: 0, y: 10 },
        ];
        expect(polygonCentroid(subdivided).y).toBeCloseTo(5, 9);
    });

    test("winding direction does not change the answer", () => {
        const reversed = [...SQUARE].reverse();
        expect(polygonCentroid(reversed).x).toBeCloseTo(5, 9);
        expect(polygonCentroid(reversed).y).toBeCloseTo(5, 9);
    });

    test("a degenerate ring falls back to the corner average", () => {
        const line: Pt[] = [
            { x: 0, y: 0 },
            { x: 4, y: 0 },
            { x: 8, y: 0 },
        ];
        expect(polygonCentroid(line).x).toBeCloseTo(4, 9);
    });

    test("an empty ring is the origin rather than NaN", () => {
        expect(polygonCentroid([])).toEqual({ x: 0, y: 0 });
    });
});

describe("interiorPoint", () => {
    test("a convex room uses its centroid", () => {
        const point = interiorPoint(SQUARE);
        expect(point.x).toBeCloseTo(5, 9);
        expect(point.y).toBeCloseTo(5, 9);
    });

    test("an L-shaped room's centroid is outside it", () => {
        // The premise of the fix: naming this room by clicking it used to drop
        // the seed in the notch, where it binds to nothing.
        expect(pointInRing(polygonCentroid(L_SHAPE), L_SHAPE)).toBe(false);
    });

    test("an L-shaped room still gets a point inside itself", () => {
        expect(pointInRing(interiorPoint(L_SHAPE), L_SHAPE)).toBe(true);
    });

    test("the point sits inside for either winding", () => {
        const reversed = [...L_SHAPE].reverse();
        expect(pointInRing(interiorPoint(reversed), reversed)).toBe(true);
    });

    test("a U-shaped room gets a point inside, not in the gap", () => {
        const u: Pt[] = [
            { x: 0, y: 0 },
            { x: 12, y: 0 },
            { x: 12, y: 10 },
            { x: 9, y: 10 },
            { x: 9, y: 3 },
            { x: 3, y: 3 },
            { x: 3, y: 10 },
            { x: 0, y: 10 },
        ];
        expect(pointInRing(interiorPoint(u), u)).toBe(true);
    });

    test("a degenerate ring does not throw", () => {
        expect(() => interiorPoint([{ x: 0, y: 0 }, { x: 1, y: 1 }])).not.toThrow();
    });
});
