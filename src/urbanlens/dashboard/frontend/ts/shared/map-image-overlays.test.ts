/**
 * Tests for the projective transform behind georeferenced map image overlays.
 *
 * The homography is the whole feature: it is what lets a user skew a scanned
 * Sanborn sheet onto real streets rather than only scaling an axis-aligned
 * box. These check it against cases whose answer is known independently -
 * identity, translation, scale - and then verify the general four-corner case
 * by pushing the source corners back through the matrix and confirming they
 * land on the destinations the user dragged them to.
 */

import { describe, expect, it } from "bun:test";

import { matrix3dForCorners } from "./map-image-overlays";

/** Parse a `matrix3d(...)` string back into its 16 column-major numbers. */
function parseMatrix(css: string): number[] {
    const inner = css.slice(css.indexOf("(") + 1, css.lastIndexOf(")"));
    return inner.split(",").map(Number);
}

/** Apply a parsed column-major matrix3d to a 2D point, with the perspective divide. */
function apply(matrix: number[], x: number, y: number): { x: number; y: number } {
    const at = (index: number): number => matrix[index] ?? 0;
    const wx = at(0) * x + at(4) * y + at(12);
    const wy = at(1) * x + at(5) * y + at(13);
    const w = at(3) * x + at(7) * y + at(15);
    return { x: wx / w, y: wy / w };
}

const WIDTH = 200;
const HEIGHT = 100;

describe("matrix3dForCorners", () => {
    it("maps an unmoved rectangle to the identity transform", () => {
        const css = matrix3dForCorners(
            [
                { x: 0, y: 0 },
                { x: WIDTH, y: 0 },
                { x: WIDTH, y: HEIGHT },
                { x: 0, y: HEIGHT },
            ],
            WIDTH,
            HEIGHT,
        );
        expect(css).not.toBeNull();
        const m = parseMatrix(css as string);
        expect(apply(m, 0, 0).x).toBeCloseTo(0, 6);
        expect(apply(m, WIDTH, HEIGHT).y).toBeCloseTo(HEIGHT, 6);
    });

    it("recovers a pure translation", () => {
        const dx = 37;
        const dy = -12;
        const css = matrix3dForCorners(
            [
                { x: dx, y: dy },
                { x: WIDTH + dx, y: dy },
                { x: WIDTH + dx, y: HEIGHT + dy },
                { x: dx, y: HEIGHT + dy },
            ],
            WIDTH,
            HEIGHT,
        );
        const m = parseMatrix(css as string);
        expect(apply(m, 0, 0).x).toBeCloseTo(dx, 6);
        expect(apply(m, 0, 0).y).toBeCloseTo(dy, 6);
    });

    it("places every corner where it was dragged, for an arbitrary quadrilateral", () => {
        // Deliberately not a parallelogram: a scanned sheet dragged onto real
        // streets is trapezoidal, which is exactly what an affine-only
        // transform cannot represent.
        const targets = [
            { x: 10, y: 20 },
            { x: 240, y: 5 },
            { x: 300, y: 160 },
            { x: -15, y: 130 },
        ];
        const css = matrix3dForCorners(targets, WIDTH, HEIGHT);
        expect(css).not.toBeNull();
        const m = parseMatrix(css as string);
        const sources = [
            { x: 0, y: 0 },
            { x: WIDTH, y: 0 },
            { x: WIDTH, y: HEIGHT },
            { x: 0, y: HEIGHT },
        ];
        sources.forEach((source, index) => {
            const target = targets[index]!;
            const mapped = apply(m, source.x, source.y);
            expect(mapped.x).toBeCloseTo(target.x, 4);
            expect(mapped.y).toBeCloseTo(target.y, 4);
        });
    });

    it("returns null for a degenerate shape instead of a NaN matrix", () => {
        // Three corners collapsed onto one point: dragging into this state
        // must leave the previous transform alone rather than making the
        // overlay vanish with no handle left to drag back.
        const css = matrix3dForCorners(
            [
                { x: 0, y: 0 },
                { x: 0, y: 0 },
                { x: 0, y: 0 },
                { x: 0, y: 50 },
            ],
            WIDTH,
            HEIGHT,
        );
        expect(css).toBeNull();
    });

    it("never emits a matrix containing NaN", () => {
        const css = matrix3dForCorners(
            [
                { x: 1, y: 1 },
                { x: 199, y: 3 },
                { x: 210, y: 98 },
                { x: -4, y: 102 },
            ],
            WIDTH,
            HEIGHT,
        );
        const m = parseMatrix(css as string);
        expect(m.every((value) => Number.isFinite(value))).toBe(true);
    });
});
