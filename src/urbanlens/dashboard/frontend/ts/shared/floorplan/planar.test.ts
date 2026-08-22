import { describe, expect, test } from "bun:test";

import { type Pt, pointInRing, signedArea } from "./coords";
import { type Segment, deriveFaces, faceForSeed } from "./planar";

/** Walls of a closed rectangle, drawn corner to corner. */
function rectangle(x0: number, y0: number, x1: number, y1: number, prefix = "r"): Segment[] {
    const corners: Pt[] = [
        { x: x0, y: y0 },
        { x: x1, y: y0 },
        { x: x1, y: y1 },
        { x: x0, y: y1 },
    ];
    return corners.map((a, i) => ({
        wallId: `${prefix}${i}`,
        a,
        b: corners[(i + 1) % corners.length] as Pt,
    }));
}

const areas = (segments: Segment[], options = {}): number[] =>
    deriveFaces(segments, options)
        .faces.map((f) => Math.round(f.area * 100) / 100)
        .sort((a, b) => a - b);

describe("deriveFaces", () => {
    test("no walls encloses nothing", () => {
        const result = deriveFaces([]);
        expect(result.faces).toEqual([]);
        expect(result.dangling).toEqual([]);
    });

    test("a single wall encloses nothing", () => {
        const result = deriveFaces([{ wallId: "w", a: { x: 0, y: 0 }, b: { x: 5, y: 0 } }]);
        expect(result.faces).toHaveLength(0);
    });

    test("a closed rectangle yields one face of the right area", () => {
        expect(areas(rectangle(0, 0, 4, 3))).toEqual([12]);
    });

    test("the derived ring is counter-clockwise", () => {
        const [face] = deriveFaces(rectangle(0, 0, 4, 3)).faces;
        expect(signedArea(face?.ring ?? [])).toBeGreaterThan(0);
    });

    test("the face records which walls bound it", () => {
        const [face] = deriveFaces(rectangle(0, 0, 4, 3)).faces;
        expect([...(face?.wallIds ?? [])].sort()).toEqual(["r0", "r1", "r2", "r3"]);
    });

    test("an open side encloses nothing", () => {
        const open = rectangle(0, 0, 4, 3).slice(0, 3); // three of four walls
        expect(deriveFaces(open, { healGap: 0 }).faces).toHaveLength(0);
    });

    test("a partition wall splits one room into two", () => {
        // Rectangle 0..4 x 0..3, with a partition at x=2 drawn corner to corner.
        const walls = [...rectangle(0, 0, 4, 3), { wallId: "p", a: { x: 2, y: 0 }, b: { x: 2, y: 3 } }];
        expect(areas(walls)).toEqual([6, 6]);
    });

    test("a partition landing mid-wall still splits the room (T-junction)", () => {
        // The partition's endpoints sit on the interior of the top and bottom
        // walls rather than at any existing corner. Without splitting those
        // walls at the touch point nothing would close.
        const walls: Segment[] = [
            { wallId: "bottom", a: { x: 0, y: 0 }, b: { x: 4, y: 0 } },
            { wallId: "right", a: { x: 4, y: 0 }, b: { x: 4, y: 3 } },
            { wallId: "top", a: { x: 4, y: 3 }, b: { x: 0, y: 3 } },
            { wallId: "left", a: { x: 0, y: 3 }, b: { x: 0, y: 0 } },
            { wallId: "partition", a: { x: 1.5, y: 0 }, b: { x: 1.5, y: 3 } },
        ];
        expect(areas(walls)).toEqual([4.5, 7.5]);
    });

    test("crossing walls produce four quadrant rooms", () => {
        const walls: Segment[] = [
            ...rectangle(0, 0, 4, 4),
            { wallId: "h", a: { x: 0, y: 2 }, b: { x: 4, y: 2 } },
            { wallId: "v", a: { x: 2, y: 0 }, b: { x: 2, y: 4 } },
        ];
        expect(areas(walls)).toEqual([4, 4, 4, 4]);
    });

    test("a room inside a room yields both faces", () => {
        const walls = [...rectangle(0, 0, 10, 10, "outer"), ...rectangle(3, 3, 6, 6, "inner")];
        expect(areas(walls)).toEqual([9, 100]);
    });

    test("a near-miss corner is healed so the room still closes", () => {
        // Left wall stops 0.2 m short of the bottom wall - a typical hand-drawn
        // miss. Default healGap is 0.6 m.
        const walls: Segment[] = [
            { wallId: "bottom", a: { x: 0, y: 0 }, b: { x: 4, y: 0 } },
            { wallId: "right", a: { x: 4, y: 0 }, b: { x: 4, y: 3 } },
            { wallId: "top", a: { x: 4, y: 3 }, b: { x: 0, y: 3 } },
            { wallId: "left", a: { x: 0, y: 3 }, b: { x: 0, y: 0.2 } },
        ];
        const result = deriveFaces(walls);
        expect(result.faces).toHaveLength(1);
        expect(result.healed).toHaveLength(1);
        expect(result.healed[0]?.gap).toBeCloseTo(0.2, 5);
    });

    test("healing does not move the walls the user drew", () => {
        const walls: Segment[] = [
            { wallId: "bottom", a: { x: 0, y: 0 }, b: { x: 4, y: 0 } },
            { wallId: "right", a: { x: 4, y: 0 }, b: { x: 4, y: 3 } },
            { wallId: "top", a: { x: 4, y: 3 }, b: { x: 0, y: 3 } },
            { wallId: "left", a: { x: 0, y: 3 }, b: { x: 0, y: 0.2 } },
        ];
        const before = structuredClone(walls);
        deriveFaces(walls);
        expect(walls).toEqual(before);
    });

    test("a gap wider than the heal tolerance is left open", () => {
        const walls: Segment[] = [
            { wallId: "bottom", a: { x: 0, y: 0 }, b: { x: 4, y: 0 } },
            { wallId: "right", a: { x: 4, y: 0 }, b: { x: 4, y: 3 } },
            { wallId: "top", a: { x: 4, y: 3 }, b: { x: 0, y: 3 } },
            { wallId: "left", a: { x: 0, y: 3 }, b: { x: 0, y: 1.5 } },
        ];
        const result = deriveFaces(walls);
        expect(result.faces).toHaveLength(0);
        expect(result.dangling.length).toBeGreaterThan(0);
    });

    test("a wall shorter than the heal gap is not bridged to itself", () => {
        // Both ends of a short stub are dangling, and they are each other's
        // nearest candidate - so the healer used to fold the wall onto its own
        // midpoint, which the editor then wrote back as a zero-length wall.
        const stub: Segment[] = [{ wallId: "stub", a: { x: 0, y: 0 }, b: { x: 0.4, y: 0 } }];
        const result = deriveFaces(stub);
        expect(result.healed).toEqual([]);
        expect(result.dangling).toHaveLength(2);
    });

    test("a doorjamb stub beside a wall still heals to its neighbour, not to itself", () => {
        const walls: Segment[] = [
            { wallId: "jamb", a: { x: 0, y: 0 }, b: { x: 0.3, y: 0 } },
            { wallId: "run", a: { x: 0.5, y: 0 }, b: { x: 4, y: 0 } },
        ];
        const result = deriveFaces(walls);
        expect(result.healed).toHaveLength(1);
        expect(result.healed[0]?.gap).toBeCloseTo(0.2, 6);
    });

    test("an overshooting wall still encloses, and the stub does not become a room", () => {
        // The right wall runs past the top wall by 0.5 m.
        const walls: Segment[] = [
            { wallId: "bottom", a: { x: 0, y: 0 }, b: { x: 4, y: 0 } },
            { wallId: "right", a: { x: 4, y: 0 }, b: { x: 4, y: 3.5 } },
            { wallId: "top", a: { x: 4, y: 3 }, b: { x: 0, y: 3 } },
            { wallId: "left", a: { x: 0, y: 3 }, b: { x: 0, y: 0 } },
        ];
        expect(areas(walls)).toEqual([12]);
    });

    test("duplicate walls drawn twice do not double the rooms", () => {
        const walls = [...rectangle(0, 0, 4, 3), ...rectangle(0, 0, 4, 3, "dup")];
        expect(areas(walls)).toEqual([12]);
    });

    test("a wall drawn in reverse still closes the room", () => {
        const walls: Segment[] = [
            { wallId: "bottom", a: { x: 0, y: 0 }, b: { x: 4, y: 0 } },
            { wallId: "right", a: { x: 4, y: 0 }, b: { x: 4, y: 3 } },
            { wallId: "top", a: { x: 0, y: 3 }, b: { x: 4, y: 3 } }, // reversed
            { wallId: "left", a: { x: 0, y: 0 }, b: { x: 0, y: 3 } }, // reversed
        ];
        expect(areas(walls)).toEqual([12]);
    });

    test("a dangling spur inside a room does not create a face", () => {
        const walls: Segment[] = [
            ...rectangle(0, 0, 6, 6),
            { wallId: "spur", a: { x: 3, y: 0 }, b: { x: 3, y: 2 } },
        ];
        expect(areas(walls)).toEqual([36]);
    });

    test("tiny slivers are discarded", () => {
        const walls = rectangle(0, 0, 0.2, 0.2);
        expect(deriveFaces(walls).faces).toHaveLength(0);
    });

    test("an L-shaped room is derived with the correct area", () => {
        const corners: Pt[] = [
            { x: 0, y: 0 },
            { x: 6, y: 0 },
            { x: 6, y: 2 },
            { x: 2, y: 2 },
            { x: 2, y: 5 },
            { x: 0, y: 5 },
        ];
        const walls = corners.map((a, i) => ({
            wallId: `l${i}`,
            a,
            b: corners[(i + 1) % corners.length] as Pt,
        }));
        expect(areas(walls)).toEqual([18]); // 6x2 bottom bar + 2x3 column above it
    });

    test("scales to a realistic floor without pathological slowdown", () => {
        // 10x10 grid of rooms: 220 wall segments, 100 derived faces.
        const walls: Segment[] = [];
        for (let i = 0; i <= 10; i++) {
            walls.push({ wallId: `h${i}`, a: { x: 0, y: i * 3 }, b: { x: 30, y: i * 3 } });
            walls.push({ wallId: `v${i}`, a: { x: i * 3, y: 0 }, b: { x: i * 3, y: 30 } });
        }
        const started = performance.now();
        const result = deriveFaces(walls);
        expect(result.faces).toHaveLength(100);
        expect(performance.now() - started).toBeLessThan(2000);
    });
});

describe("faceForSeed", () => {
    const walls = [...rectangle(0, 0, 4, 3), { wallId: "p", a: { x: 2, y: 0 }, b: { x: 2, y: 3 } }];
    const faces = deriveFaces(walls).faces;

    test("binds a seed to the room containing it", () => {
        const left = faceForSeed({ x: 1, y: 1.5 }, faces);
        const right = faceForSeed({ x: 3, y: 1.5 }, faces);
        expect(left).not.toBeNull();
        expect(right).not.toBeNull();
        expect(left).not.toBe(right);
        expect(pointInRing({ x: 1, y: 1.5 }, left?.ring ?? [])).toBe(true);
    });

    test("a seed outside every room binds to nothing", () => {
        expect(faceForSeed({ x: 100, y: 100 }, faces)).toBeNull();
    });

    test("nested rooms bind the seed to the smallest containing face", () => {
        const nested = deriveFaces([...rectangle(0, 0, 10, 10, "outer"), ...rectangle(3, 3, 6, 6, "inner")]).faces;
        const bound = faceForSeed({ x: 4, y: 4 }, nested);
        expect(bound?.area).toBeCloseTo(9, 5);
    });

    test("a seed keeps its room when an unrelated wall elsewhere moves", () => {
        // Room identity is a seed point, so edits elsewhere cannot reassign it.
        const moved = [...walls, { wallId: "far", a: { x: 20, y: 20 }, b: { x: 25, y: 20 } }];
        const after = deriveFaces(moved).faces;
        const bound = faceForSeed({ x: 1, y: 1.5 }, after);
        expect(bound?.area).toBeCloseTo(6, 5);
    });
});
