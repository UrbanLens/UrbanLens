import { describe, expect, test } from "bun:test";

import type { Opening, Wall } from "./document";
import { rehostOpening } from "./openings";

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
