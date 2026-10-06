/**
 * The geometry edits behind the composer's handles: extending a line, adding, moving and removing
 * points, and moving, scaling and turning a whole shape.
 */
import { describe, expect, test } from "bun:test";
import type { LatLngTuple, ShapeSpec } from "./markup-engine";
import {
    angleFrom,
    boxCenter,
    canRemoveVertex,
    canRotate,
    canScale,
    edgeMidpoints,
    editablePoints,
    extendPoints,
    insertVertex,
    moveEditablePoint,
    rectCorners,
    removeVertex,
    rotateShape,
    scaleShape,
    shapeBox,
    translateShape,
    type Projection,
} from "./markup-geometry";

/** A flat projection, 1000 px to the degree, y down as on screen - enough to check the arithmetic. */
const proj: Projection = {
    project: (ll) => ({ x: ll[1] * 1000, y: -ll[0] * 1000 }),
    unproject: (p) => [-p.y / 1000, p.x / 1000],
};

function near(actual: LatLngTuple[], expected: LatLngTuple[]): void {
    expect(actual).toHaveLength(expected.length);
    actual.forEach((ll, i) => {
        expect(ll[0]).toBeCloseTo(expected[i]![0], 9);
        expect(ll[1]).toBeCloseTo(expected[i]![1], 9);
    });
}

const LINE: ShapeSpec = { type: "line", latlngs: [[0, 0], [0, 1], [1, 1]], color: "#000000", stroke_width: 3 };
const ARROW: ShapeSpec = { type: "arrow", latlngs: [[0, 0], [0, 1]], color: "#e74c3c", stroke_width: 3 };
const POLYGON: ShapeSpec = { type: "polygon", latlngs: [[0, 0], [0, 1], [1, 1]], color: "#000000" };
const RECT: ShapeSpec = { type: "rect", latlngs: [[0, 0], [1, 2]], color: "#000000" };
const CIRCLE: ShapeSpec = { type: "circle", latlngs: [[0, 0], [0, 0.5]], color: "#000000" };
const TEXT: ShapeSpec = { type: "text", latlngs: [[0, 0]], color: "#000000", stroke_width: 16, label: "Gate" };
const PIN: ShapeSpec = { type: "pin", latlngs: [[0, 0]], color: "#000000" };

describe("what each shape can do", () => {
    test("points can be removed down to two on a line and three on a polygon", () => {
        expect(canRemoveVertex(LINE)).toBe(true);
        expect(canRemoveVertex(ARROW)).toBe(false);
        expect(canRemoveVertex(POLYGON)).toBe(false);
        expect(canRemoveVertex({ ...POLYGON, latlngs: [...POLYGON.latlngs, [1, 0]] })).toBe(true);
        expect(canRemoveVertex(RECT)).toBe(false);
    });

    test("a pin neither scales nor turns; a circle scales but has no turn", () => {
        expect(canScale(PIN)).toBe(false);
        expect(canRotate(PIN)).toBe(false);
        expect(canScale(CIRCLE)).toBe(true);
        expect(canRotate(CIRCLE)).toBe(false);
        expect(canRotate(TEXT)).toBe(true);
        expect(canRotate(RECT)).toBe(true);
    });

    test("the grabbable points: a rectangle's four corners, a label's anchor, a line's vertices", () => {
        expect(editablePoints(RECT)).toEqual([[0, 0], [0, 2], [1, 2], [1, 0]]);
        expect(editablePoints(TEXT)).toEqual([[0, 0]]);
        expect(editablePoints(LINE)).toEqual(LINE.latlngs);
    });
});

describe("adding, moving and removing points", () => {
    test("a new point goes where it is put, and never past either end", () => {
        expect(insertVertex(LINE, 1, [0.5, 0.5]).latlngs).toEqual([[0, 0], [0.5, 0.5], [0, 1], [1, 1]]);
        expect(insertVertex(LINE, 99, [2, 2]).latlngs.at(-1)).toEqual([2, 2]);
        expect(insertVertex(LINE, -5, [2, 2]).latlngs[0]).toEqual([2, 2]);
    });

    test("a circle, rectangle or label takes no new points", () => {
        expect(insertVertex(CIRCLE, 1, [5, 5])).toEqual(CIRCLE);
        expect(insertVertex(RECT, 1, [5, 5])).toEqual(RECT);
    });

    test("removing a point is refused where it would leave too few", () => {
        expect(removeVertex(LINE, 1)?.latlngs).toEqual([[0, 0], [1, 1]]);
        expect(removeVertex(ARROW, 0)).toBeNull();
        expect(removeVertex(LINE, 7)).toBeNull();
    });

    test("an edit never changes the shape it was given", () => {
        const before = JSON.stringify(LINE);
        insertVertex(LINE, 1, [9, 9]);
        removeVertex(LINE, 1);
        moveEditablePoint(LINE, 0, [9, 9], proj);
        translateShape(LINE, 10, 10, proj);
        expect(JSON.stringify(LINE)).toBe(before);
    });

    test("a vertex moves on its own", () => {
        expect(moveEditablePoint(LINE, 2, [2, 2], proj).latlngs).toEqual([[0, 0], [0, 1], [2, 2]]);
    });

    test("a rectangle's corner moves with the opposite corner held", () => {
        // Corner 2 is the second point drawn; corner 0 opposite it stays put.
        expect(moveEditablePoint(RECT, 2, [3, 3], proj).latlngs).toEqual([[0, 0], [3, 3]]);
        // Corner 1 is [0, 2]; its opposite is [1, 0].
        expect(moveEditablePoint(RECT, 1, [-1, 4], proj).latlngs).toEqual([[1, 0], [-1, 4]]);
    });

    test("a circle's centre carries the circle; its edge point sets the radius", () => {
        near(moveEditablePoint(CIRCLE, 0, [1, 1], proj).latlngs, [[1, 1], [1, 1.5]]);
        expect(moveEditablePoint(CIRCLE, 1, [0, 2], proj).latlngs).toEqual([[0, 0], [0, 2]]);
    });

    test("a label moves whole", () => {
        near(moveEditablePoint({ ...TEXT, latlngs: [[0, 0], [1, 1]] }, 0, [2, 2], proj).latlngs, [[2, 2], [3, 3]]);
    });

    test("an out-of-range point index changes nothing", () => {
        expect(moveEditablePoint(LINE, 9, [5, 5], proj)).toEqual(LINE);
    });

    test("each edge has a midpoint to add a point at, closing edge included on a polygon", () => {
        const line = edgeMidpoints(LINE, proj);
        expect(line.map((m) => m.index)).toEqual([1, 2]);
        near(line.map((m) => m.ll), [[0, 0.5], [0.5, 1]]);
        const polygon = edgeMidpoints(POLYGON, proj);
        expect(polygon.map((m) => m.index)).toEqual([1, 2, 3]);
        near([polygon[2]!.ll], [[0.5, 0.5]]);
        expect(edgeMidpoints(RECT, proj)).toEqual([]);
    });

    test("a line or arrow can be pulled longer from beyond either end", () => {
        const ends = extendPoints(LINE, proj, 100)!;
        // Start: back along the first segment (westwards); end: on along the last (northwards).
        near([ends.start, ends.end], [[0, -0.1], [1.1, 1]]);
        expect(extendPoints(POLYGON, proj, 100)).toBeNull();
    });
});

describe("moving, scaling and turning a whole shape", () => {
    test("moving shifts every point by the same screen distance", () => {
        near(translateShape(LINE, 1000, -1000, proj).latlngs, [[1, 1], [1, 2], [2, 2]]);
    });

    test("scaling is about the origin given", () => {
        near(scaleShape(ARROW, 2, { x: 0, y: 0 }, proj).latlngs, [[0, 0], [0, 2]]);
        near(scaleShape(CIRCLE, 3, { x: 0, y: 0 }, proj).latlngs, [[0, 0], [0, 1.5]]);
    });

    test("a nonsensical scale changes nothing, and a pin never scales", () => {
        expect(scaleShape(ARROW, 0, { x: 0, y: 0 }, proj)).toEqual(ARROW);
        expect(scaleShape(ARROW, Number.NaN, { x: 0, y: 0 }, proj)).toEqual(ARROW);
        expect(scaleShape(PIN, 2, { x: 0, y: 0 }, proj)).toEqual(PIN);
    });

    test("scaling a label changes its text size, within the sizes the renderer draws", () => {
        expect(scaleShape(TEXT, 2, { x: 0, y: 0 }, proj).stroke_width).toBe(32);
        expect(scaleShape(TEXT, 100, { x: 0, y: 0 }, proj).stroke_width).toBe(96);
        expect(scaleShape(TEXT, 0.01, { x: 0, y: 0 }, proj).stroke_width).toBe(8);
        // Its anchor stays where it was put.
        expect(scaleShape(TEXT, 2, { x: 500, y: 500 }, proj).latlngs).toEqual([[0, 0]]);
    });

    test("turning is clockwise as seen on screen", () => {
        // An arrow pointing east, turned 90 degrees clockwise about its tail, points south.
        near(rotateShape(ARROW, 90, { x: 0, y: 0 }, proj).latlngs, [[0, 0], [-1, 0]]);
    });

    test("a turned rectangle becomes a four-sided polygon", () => {
        const turned = rotateShape(RECT, 90, boxCenter(shapeBox(RECT, proj)), proj);
        expect(turned.type).toBe("polygon");
        expect(turned.latlngs).toHaveLength(4);
        // Turned a quarter about its centre (lng 1, lat 0.5), the 2-wide, 1-tall box is 1 wide and 2 tall.
        const lats = turned.latlngs.map((ll) => ll[0]);
        const lngs = turned.latlngs.map((ll) => ll[1]);
        expect(Math.max(...lats) - Math.min(...lats)).toBeCloseTo(2, 9);
        expect(Math.max(...lngs) - Math.min(...lngs)).toBeCloseTo(1, 9);
    });

    test("a label turns about itself, kept in -180..180, and a full turn clears it", () => {
        expect(rotateShape(TEXT, 30, { x: 0, y: 0 }, proj).rotation).toBe(30);
        expect(rotateShape({ ...TEXT, rotation: 170 }, 30, { x: 0, y: 0 }, proj).rotation).toBe(-160);
        expect(rotateShape({ ...TEXT, rotation: 30 }, -30, { x: 0, y: 0 }, proj)).not.toHaveProperty("rotation");
        expect(rotateShape(TEXT, 30, { x: 999, y: 999 }, proj).latlngs).toEqual(TEXT.latlngs);
    });

    test("a circle and a pin look the same however turned", () => {
        expect(rotateShape(CIRCLE, 45, { x: 100, y: 100 }, proj)).toEqual(CIRCLE);
        expect(rotateShape(PIN, 45, { x: 100, y: 100 }, proj)).toEqual(PIN);
    });
});

describe("boxes and angles", () => {
    test("a circle's box is its centre plus and minus its radius", () => {
        expect(shapeBox(CIRCLE, proj)).toEqual({ minX: -500, minY: -500, maxX: 500, maxY: 500 });
    });

    test("a rectangle's box spans its corners", () => {
        expect(shapeBox(RECT, proj)).toEqual({ minX: 0, minY: -1000, maxX: 2000, maxY: -0 });
    });

    test("a label's box is its drawn size from the anchor; a pin's sits above its point", () => {
        expect(shapeBox(TEXT, proj, { width: 40, height: 20 })).toEqual({ minX: 0, minY: -0, maxX: 40, maxY: 20 });
        expect(shapeBox(PIN, proj)).toEqual({ minX: -16, minY: -32, maxX: 16, maxY: -0 });
    });

    test("angles are measured clockwise from straight up", () => {
        expect(angleFrom({ x: 0, y: 0 }, { x: 0, y: -10 })).toBeCloseTo(0, 9);
        expect(angleFrom({ x: 0, y: 0 }, { x: 10, y: 0 })).toBeCloseTo(90, 9);
        expect(angleFrom({ x: 0, y: 0 }, { x: -10, y: 0 })).toBeCloseTo(-90, 9);
    });

    test("rectangle corners go round from the first point drawn", () => {
        expect(rectCorners(RECT)).toEqual([[0, 0], [0, 2], [1, 2], [1, 0]]);
    });
});
