import { describe, expect, test } from "bun:test";

import type { Opening, Wall } from "./document";
import { doorLeaves, rehostOpening, swings } from "./openings";

function wall(uuid: string, length: number, openings: Opening[] = []): Wall {
    return { uuid, kind: "interior", thickness: "normal", ax: 0, ay: 0, bx: length, by: 0, openings };
}

function door(t_start: number, t_end: number): Opening {
    return { uuid: "door", kind: "door", t_start, t_end, swing: "left" };
}

/** The opening's width in metres on the wall it currently sits in. */
function widthOn(host: Wall, opening: Opening): number {
    return (opening.t_end - opening.t_start) * (host.bx - host.ax);
}

describe("rehostOpening", () => {
    test("the door keeps its metre width, not its fractions", () => {
        // 0.9m in a 10m wall is 0.09 of it; the same fractions in a 3m wall
        // would be a 27cm door.
        const from = wall("long", 10);
        const opening = door(0.455, 0.545);
        from.openings.push(opening);
        const to = wall("short", 3);

        expect(rehostOpening(opening, from, to, 1.5)).toBe(true);

        expect(widthOn(to, opening)).toBeCloseTo(0.9, 5);
    });

    test("it leaves the wall it came from", () => {
        const from = wall("long", 10);
        const opening = door(0.4, 0.5);
        from.openings.push(opening);
        const to = wall("other", 10);

        rehostOpening(opening, from, to, 5);

        expect(from.openings).toEqual([]);
        expect(to.openings).toEqual([opening]);
    });

    test("a door wider than its new wall is cut down to fit", () => {
        const from = wall("long", 10);
        const opening = door(0.1, 0.9); // 8m
        from.openings.push(opening);
        const to = wall("stub", 2);

        rehostOpening(opening, from, to, 1);

        expect(opening.t_start).toBeGreaterThanOrEqual(0);
        expect(opening.t_end).toBeLessThanOrEqual(1);
        expect(widthOn(to, opening)).toBeLessThanOrEqual(2 + 1e-9);
    });

    test("a centre past the end slides back inside rather than hanging off", () => {
        const from = wall("long", 10);
        const opening = door(0.4, 0.49); // 0.9m
        from.openings.push(opening);
        const to = wall("other", 4);

        rehostOpening(opening, from, to, 99);

        expect(opening.t_end).toBeLessThanOrEqual(1);
        expect(widthOn(to, opening)).toBeCloseTo(0.9, 5);
    });

    test("a centre before the start does too", () => {
        const from = wall("long", 10);
        const opening = door(0.4, 0.49);
        from.openings.push(opening);
        const to = wall("other", 4);

        rehostOpening(opening, from, to, -20);

        expect(opening.t_start).toBeGreaterThanOrEqual(0);
        expect(widthOn(to, opening)).toBeCloseTo(0.9, 5);
    });

    test("moving within the same wall does not lose the opening", () => {
        // `from.openings` is reassigned before the push, and when the target is
        // the same wall that is the array being pushed onto.
        const host = wall("only", 10);
        const opening = door(0.1, 0.19);
        host.openings.push(opening);

        rehostOpening(opening, host, host, 8);

        expect(host.openings).toEqual([opening]);
        expect(widthOn(host, opening)).toBeCloseTo(0.9, 5);
        expect(opening.t_start).toBeGreaterThan(0.5);
    });

    test("a wall with no length refuses the move and changes nothing", () => {
        const from = wall("long", 10);
        const opening = door(0.4, 0.49);
        from.openings.push(opening);
        const to = wall("degenerate", 0);

        expect(rehostOpening(opening, from, to, 0)).toBe(false);

        expect(from.openings).toEqual([opening]);
        expect(to.openings).toEqual([]);
        expect(opening.t_start).toBe(0.4);
    });

    test("the openings already on the target wall are kept", () => {
        const from = wall("long", 10);
        const opening = door(0.4, 0.49);
        from.openings.push(opening);
        const sitting = { uuid: "window", kind: "window", t_start: 0.1, t_end: 0.2, swing: "none" } as Opening;
        const to = wall("other", 10, [sitting]);

        rehostOpening(opening, from, to, 5);

        expect(to.openings).toEqual([sitting, opening]);
    });
});

describe("swings", () => {
    test("only a door and a gate have a leaf", () => {
        expect(swings("door")).toBe(true);
        expect(swings("gate")).toBe(true);
        // A doorway is the hole with no door in it.
        expect(swings("doorway")).toBe(false);
        expect(swings("window")).toBe(false);
        expect(swings("hatch")).toBe(false);
    });
});

describe("doorLeaves", () => {
    /** A 10m wall running east from the origin, with a 1m door at its middle. */
    function eastWall(swing: Opening["swing"], kind: Opening["kind"] = "door"): { wall: Wall; opening: Opening } {
        const opening = { uuid: "d", kind, t_start: 0.45, t_end: 0.55, swing } as Opening;
        return { wall: wall("east", 10, [opening]), opening };
    }

    test("an unknown swing draws nothing", () => {
        const { wall: host, opening } = eastWall("none");
        expect(doorLeaves(host, opening)).toEqual([]);
    });

    test("a kind with no leaf draws nothing even if a swing was stored", () => {
        const { wall: host, opening } = eastWall("left", "window");
        expect(doorLeaves(host, opening)).toEqual([]);
    });

    test("a single door gives one leaf, a double gives two", () => {
        const single = eastWall("left");
        expect(doorLeaves(single.wall, single.opening)).toHaveLength(1);
        const dbl = eastWall("double");
        expect(doorLeaves(dbl.wall, dbl.opening)).toHaveLength(2);
    });

    test("the leaf starts and ends at its hinge", () => {
        const { wall: host, opening } = eastWall("left");
        const leaf = doorLeaves(host, opening)[0] as { x: number; y: number }[];

        const hinge = { x: 4.5, y: 0 };
        expect(leaf[0]?.x).toBeCloseTo(hinge.x + 1, 5); // flat along the wall
        expect(leaf[0]?.y).toBeCloseTo(0, 5);
        expect(leaf[leaf.length - 1]).toEqual(hinge);
    });

    test("the leaf is as long as the door is wide", () => {
        const { wall: host, opening } = eastWall("left");
        const leaf = doorLeaves(host, opening)[0] as { x: number; y: number }[];
        const hinge = { x: 4.5, y: 0 };

        for (const point of leaf.slice(0, -1)) {
            expect(Math.hypot(point.x - hinge.x, point.y - hinge.y)).toBeCloseTo(1, 5);
        }
    });

    test("hinged at the end swings from the other side", () => {
        const { wall: host, opening } = eastWall("right");
        const leaf = doorLeaves(host, opening)[0] as { x: number; y: number }[];

        expect(leaf[leaf.length - 1]).toEqual({ x: 5.5, y: 0 });
        // Flat along the wall means back towards the door's other end.
        expect(leaf[0]?.x).toBeCloseTo(4.5, 5);
    });

    test("both leaves of a double door are half the width", () => {
        const { wall: host, opening } = eastWall("double");
        const [first, second] = doorLeaves(host, opening) as Array<{ x: number; y: number }[]>;

        expect(Math.hypot((first?.[0]?.x ?? 0) - 4.5, first?.[0]?.y ?? 0)).toBeCloseTo(0.5, 5);
        expect(Math.hypot((second?.[0]?.x ?? 0) - 5.5, second?.[0]?.y ?? 0)).toBeCloseTo(0.5, 5);
    });

    test("a wall with no length draws nothing rather than dividing by zero", () => {
        const opening = { uuid: "d", kind: "door", t_start: 0.4, t_end: 0.6, swing: "left" } as Opening;
        expect(doorLeaves(wall("degenerate", 0, [opening]), opening)).toEqual([]);
    });

    test("the arc sweeps off the wall, not along it", () => {
        const { wall: host, opening } = eastWall("left");
        const leaf = doorLeaves(host, opening)[0] as { x: number; y: number }[];
        const midpoint = leaf[Math.floor((leaf.length - 1) / 2)] as { x: number; y: number };

        expect(Math.abs(midpoint.y)).toBeGreaterThan(0.5);
    });
});
