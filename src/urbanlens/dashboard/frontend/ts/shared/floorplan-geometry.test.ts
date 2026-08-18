import { describe, expect, test } from "bun:test";

import {
    closeRing,
    dropTrailingDuplicates,
    insertVertex,
    isClosedRing,
    midpoint,
    minimumVertices,
    moveVertex,
    removeVertex,
    ringOf,
    samePoint,
    vertexCount,
    type Position,
} from "./floorplan-geometry";

const square = () => ({
    type: "Polygon",
    coordinates: [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]] as Position[]],
});
const line = () => ({ type: "LineString", coordinates: [[0, 0], [1, 0], [2, 0]] as Position[] });

describe("ring access", () => {
    test("a polygon's editable ring is its outer ring", () => {
        expect(ringOf(square())).toHaveLength(5);
        expect(isClosedRing(square())).toBe(true);
    });

    test("a point has no editable ring", () => {
        expect(ringOf({ type: "Point", coordinates: [0, 0] })).toBeNull();
        expect(vertexCount({ type: "Point", coordinates: [0, 0] })).toBe(0);
    });

    test("the closing repeat is not a vertex the user can see", () => {
        expect(vertexCount(square())).toBe(4);
        expect(vertexCount(line())).toBe(3);
    });
});

describe("moveVertex", () => {
    test("moving the first vertex of a closed ring moves the repeat too", () => {
        const geometry = square();

        moveVertex(geometry, 0, [9, 9]);

        const ring = ringOf(geometry) as Position[];
        expect(ring[0]).toEqual([9, 9]);
        expect(ring[ring.length - 1]).toEqual([9, 9]);
    });

    test("a ring whose ends drift apart is not a polygon GEOS will accept", () => {
        const geometry = square();

        moveVertex(geometry, 2, [5, 5]);

        const ring = ringOf(geometry) as Position[];
        expect(ring[0]).toEqual(ring[ring.length - 1] as Position);
    });

    test("an out-of-range index changes nothing", () => {
        const geometry = square();

        expect(moveVertex(geometry, 4, [9, 9])).toBe(false);
        expect(ringOf(geometry)?.[4]).toEqual([0, 0]);
    });
});

describe("removeVertex", () => {
    test("a room may not drop below three corners", () => {
        const triangle = { type: "Polygon", coordinates: [[[0, 0], [1, 0], [0, 1], [0, 0]] as Position[]] };

        expect(removeVertex(triangle, 0)).toBe(false);
        expect(vertexCount(triangle)).toBe(3);
    });

    test("a wall may not drop below two points", () => {
        const segment = { type: "LineString", coordinates: [[0, 0], [1, 0]] as Position[] };

        expect(removeVertex(segment, 0)).toBe(false);
    });

    test("removing the first corner keeps the ring closed on the new first", () => {
        const geometry = square();

        expect(removeVertex(geometry, 0)).toBe(true);

        const ring = ringOf(geometry) as Position[];
        expect(ring[0]).toEqual([1, 0]);
        expect(ring[ring.length - 1]).toEqual([1, 0]);
        expect(vertexCount(geometry)).toBe(3);
    });

    test("minimums are per shape", () => {
        expect(minimumVertices(square())).toBe(3);
        expect(minimumVertices(line())).toBe(2);
    });
});

describe("insertVertex", () => {
    test("a midpoint drag inserts after its own vertex", () => {
        const geometry = line();

        insertVertex(geometry, 0, [0.5, 0]);

        expect(ringOf(geometry)).toEqual([[0, 0], [0.5, 0], [1, 0], [2, 0]]);
    });

    test("the midpoint is halfway between", () => {
        expect(midpoint([0, 0], [2, 4])).toEqual([1, 2]);
    });
});

describe("click sequences", () => {
    test("the double-click that finishes a shape does not leave a zero-length segment", () => {
        const points: Position[] = [[0, 0], [1, 0], [1, 1], [1, 1]];

        expect(dropTrailingDuplicates(points)).toEqual([[0, 0], [1, 0], [1, 1]]);
    });

    test("distinct points are left alone", () => {
        const points: Position[] = [[0, 0], [1, 0], [1, 1]];

        expect(dropTrailingDuplicates(points)).toEqual(points);
    });

    test("closing a ring repeats the first point", () => {
        expect(closeRing([[0, 0], [1, 0], [1, 1]])).toEqual([[0, 0], [1, 0], [1, 1], [0, 0]]);
    });

    test("an already-closed ring is not double-closed", () => {
        expect(closeRing([[0, 0], [1, 0], [0, 0]])).toHaveLength(3);
    });

    test("samePoint tolerates float noise but not real distance", () => {
        expect(samePoint([1, 1], [1 + 1e-12, 1])).toBe(true);
        expect(samePoint([1, 1], [1.0001, 1])).toBe(false);
    });
});
