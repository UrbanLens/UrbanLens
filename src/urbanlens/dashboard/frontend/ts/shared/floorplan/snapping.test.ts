import { describe, expect, test } from "bun:test";

import type { Pt } from "./coords";
import type { Segment } from "./planar";
import { ANGLE_STEP_RADIANS, clampOpening, parameterAlong, shouldRelease, snapPoint, snapTranslation, tolerancesFor } from "./snapping";

const TOL = { endpoint: 0.5, wall: 0.35, extension: 0.35 };

const wall = (ax: number, ay: number, bx: number, by: number, id = "w"): Segment => ({
    wallId: id,
    a: { x: ax, y: ay },
    b: { x: bx, y: by },
});

describe("snapPoint", () => {
    test("an isolated cursor is left alone", () => {
        const snap = snapPoint({ x: 50, y: 50 }, [wall(0, 0, 10, 0)], TOL);
        expect(snap.kind).toBe("free");
        expect(snap.point).toEqual({ x: 50, y: 50 });
    });

    test("a cursor near a corner lands exactly on it", () => {
        const snap = snapPoint({ x: 10.2, y: 0.15 }, [wall(0, 0, 10, 0)], TOL);
        expect(snap.kind).toBe("endpoint");
        expect(snap.point).toEqual({ x: 10, y: 0 });
    });

    test("an endpoint beats a wall when both are in range", () => {
        // Sitting near the shared corner of two walls: the corner is the intent.
        const snap = snapPoint({ x: 0.1, y: 0.1 }, [wall(0, 0, 10, 0), wall(0, 0, 0, 10, "v")], TOL);
        expect(snap.kind).toBe("endpoint");
        expect(snap.point).toEqual({ x: 0, y: 0 });
    });

    test("a cursor near a wall's middle lands on the wall", () => {
        const snap = snapPoint({ x: 5, y: 0.2 }, [wall(0, 0, 10, 0)], TOL);
        expect(snap.kind).toBe("wall");
        expect(snap.point.x).toBeCloseTo(5, 6);
        expect(snap.point.y).toBeCloseTo(0, 6);
    });

    test("a T-junction snaps to the wall it lands on, so the region can close", () => {
        const snap = snapPoint({ x: 3, y: 0.1 }, [wall(0, 0, 10, 0)], TOL);
        expect(snap.kind).toBe("wall");
        expect(snap.point.y).toBeCloseTo(0, 6);
    });

    test("a cursor past a wall's end snaps to its extension", () => {
        const snap = snapPoint({ x: 13, y: 0.1 }, [wall(0, 0, 10, 0)], TOL);
        expect(snap.kind).toBe("extension");
        expect(snap.point.y).toBeCloseTo(0, 6);
        expect(snap.point.x).toBeCloseTo(13, 6);
    });

    test("suspending returns the raw cursor even on top of a corner", () => {
        const snap = snapPoint({ x: 10.1, y: 0.1 }, [wall(0, 0, 10, 0)], TOL, { suspended: true });
        expect(snap.kind).toBe("free");
        expect(snap.point).toEqual({ x: 10.1, y: 0.1 });
    });

    test("a near-horizontal drag squares to the axis", () => {
        const snap = snapPoint({ x: 5, y: 0.1 }, [], TOL, { from: { x: 0, y: 0 } });
        expect(snap.kind).toBe("angle");
        expect(snap.point.y).toBeCloseTo(0, 6);
    });

    test("angle snapping rounds the length to a quarter metre", () => {
        const snap = snapPoint({ x: 5.13, y: 0.02 }, [], TOL, { from: { x: 0, y: 0 } });
        expect(snap.kind).toBe("angle");
        expect(snap.point.x).toBeCloseTo(5.25, 6);
    });

    test("a deliberately skewed wall is left skewed", () => {
        // 20 degrees off axis is well outside the capture arc.
        const cursor: Pt = { x: Math.cos((20 * Math.PI) / 180) * 5, y: Math.sin((20 * Math.PI) / 180) * 5 };
        const snap = snapPoint(cursor, [], TOL, { from: { x: 0, y: 0 } });
        expect(snap.kind).toBe("free");
    });

    test("the axis rotates with the building", () => {
        // With a 30 degree drawing axis, a 30 degree drag is "square".
        const angle = (30 * Math.PI) / 180;
        const cursor: Pt = { x: Math.cos(angle) * 4, y: Math.sin(angle) * 4 };
        const snap = snapPoint(cursor, [], TOL, { from: { x: 0, y: 0 }, axisRadians: angle });
        expect(snap.kind).toBe("angle");
        expect(snap.point.x).toBeCloseTo(cursor.x, 6);
        expect(snap.point.y).toBeCloseTo(cursor.y, 6);
    });

    test("angle snapping never reports a wrapped angle as far away", () => {
        // Straight back along the axis is 180 degrees, which naive subtraction
        // makes look like a 2*pi error rather than an exact hit.
        const snap = snapPoint({ x: -5, y: 0.01 }, [], TOL, { from: { x: 0, y: 0 } });
        expect(snap.kind).toBe("angle");
        expect(snap.point.y).toBeCloseTo(0, 6);
    });

    test("every 45 degree increment is capturable", () => {
        for (let step = 0; step < 8; step++) {
            const angle = step * ANGLE_STEP_RADIANS;
            const cursor: Pt = { x: Math.cos(angle) * 3, y: Math.sin(angle) * 3 };
            expect(snapPoint(cursor, [], TOL, { from: { x: 0, y: 0 } }).kind).toBe("angle");
        }
    });

    test("no walls and no chain in progress is always free", () => {
        expect(snapPoint({ x: 1, y: 2 }, [], TOL).kind).toBe("free");
    });
});

describe("tolerancesFor", () => {
    test("zooming in shrinks the metre tolerance", () => {
        const wide = tolerancesFor(0.5);
        const close = tolerancesFor(0.05);
        expect(close.endpoint).toBeLessThan(wide.endpoint);
    });

    test("the pixel feel is constant across zoom", () => {
        // 12 px at any scale: the ratio between tolerances is the scale ratio.
        expect(tolerancesFor(0.5).endpoint / tolerancesFor(0.05).endpoint).toBeCloseTo(10, 6);
    });
});

describe("shouldRelease", () => {
    test("a small wobble keeps the snap", () => {
        expect(shouldRelease({ x: 0, y: 0 }, { x: 0.3, y: 0 }, 0.5)).toBe(false);
    });

    test("moving well past the tolerance releases it", () => {
        expect(shouldRelease({ x: 0, y: 0 }, { x: 1.2, y: 0 }, 0.5)).toBe(true);
    });

    test("the release threshold sits above the capture threshold", () => {
        // Exactly at the tolerance the snap must hold, or it flickers.
        expect(shouldRelease({ x: 0, y: 0 }, { x: 0.5, y: 0 }, 0.5)).toBe(false);
    });
});

describe("parameterAlong", () => {
    test("the midpoint of a wall is one half", () => {
        expect(parameterAlong({ x: 5, y: 0 }, { a: { x: 0, y: 0 }, b: { x: 10, y: 0 } })).toBeCloseTo(0.5, 6);
    });

    test("a point past the end clamps into range", () => {
        expect(parameterAlong({ x: 99, y: 0 }, { a: { x: 0, y: 0 }, b: { x: 10, y: 0 } })).toBe(1);
    });
});

describe("clampOpening", () => {
    test("a valid interval is unchanged", () => {
        expect(clampOpening(0.25, 0.75)).toEqual([0.25, 0.75]);
    });

    test("a reversed drag is put back in order", () => {
        const [start, end] = clampOpening(0.8, 0.2);
        expect(start).toBeLessThan(end);
    });

    test("an interval running past the wall is pulled inside", () => {
        const [start, end] = clampOpening(0.5, 1.4);
        expect(end).toBeLessThanOrEqual(1);
        expect(start).toBeLessThan(end);
    });

    test("a negative start is pulled to zero", () => {
        const [start] = clampOpening(-0.3, 0.5);
        expect(start).toBeGreaterThanOrEqual(0);
    });

    test("a collapsed opening keeps a minimum width so it stays grabbable", () => {
        const [start, end] = clampOpening(0.5, 0.5);
        expect(end - start).toBeGreaterThan(0);
    });

    test("every result satisfies the database constraint", () => {
        for (const [a, b] of [[-1, 2], [0.9, 0.1], [0, 0], [1, 1], [0.5, 0.5001]]) {
            const [start, end] = clampOpening(a as number, b as number);
            expect(start).toBeGreaterThanOrEqual(0);
            expect(end).toBeLessThanOrEqual(1);
            expect(start).toBeLessThan(end);
        }
    });
});

describe("snapTranslation", () => {
    const WIDE = { endpoint: 1, wall: 0.5, extension: 0.5 };
    const wall = (id: string, ax: number, ay: number, bx: number, by: number): Segment => ({ wallId: id, a: { x: ax, y: ay }, b: { x: bx, y: by } });

    test("no candidates leaves the delta alone", () => {
        expect(snapTranslation([{ x: 0, y: 0 }], { x: 3, y: 4 }, [], WIDE)).toEqual({ x: 3, y: 4 });
    });

    test("nothing carried leaves the delta alone", () => {
        expect(snapTranslation([], { x: 3, y: 4 }, [wall("w", 0, 0, 5, 0)], WIDE)).toEqual({ x: 3, y: 4 });
    });

    test("a carried point landing near a corner is pulled onto it exactly", () => {
        // Moving (0,0) by (4.8, 0) lands 0.2 short of the corner at (5,0).
        const result = snapTranslation([{ x: 0, y: 0 }], { x: 4.8, y: 0 }, [wall("w", 5, 0, 5, 5)], WIDE);
        expect(result.x).toBeCloseTo(5, 6);
        expect(result.y).toBeCloseTo(0, 6);
    });

    test("a delta out of reach of everything is untouched", () => {
        const result = snapTranslation([{ x: 0, y: 0 }], { x: 1, y: 0 }, [wall("w", 50, 50, 50, 55)], WIDE);
        expect(result).toEqual({ x: 1, y: 0 });
    });

    test("the whole group moves by one delta, so it keeps its shape", () => {
        const corners = [
            { x: 0, y: 0 },
            { x: 2, y: 0 },
            { x: 2, y: 2 },
            { x: 0, y: 2 },
        ];
        const delta = snapTranslation(corners, { x: 4.8, y: 0 }, [wall("target", 5, 0, 5, 5)], WIDE);
        const moved = corners.map((c) => ({ x: c.x + delta.x, y: c.y + delta.y }));
        // Same box, just translated: opposite sides still 2 apart.
        expect(moved[1]!.x - moved[0]!.x).toBeCloseTo(2, 9);
        expect(moved[3]!.y - moved[0]!.y).toBeCloseTo(2, 9);
    });

    test("the smallest correction among the carried points wins", () => {
        // (0,0)+delta is 0.6 from a corner; (10,0)+delta is 0.1 from another.
        const segments = [wall("near", 10.9, 0, 10.9, 3), wall("far", 0.4, 0, 0.4, 3)];
        const result = snapTranslation([{ x: 0, y: 0 }, { x: 10, y: 0 }], { x: 1, y: 0 }, segments, WIDE);
        expect(result.x).toBeCloseTo(0.9, 6);
    });
});
