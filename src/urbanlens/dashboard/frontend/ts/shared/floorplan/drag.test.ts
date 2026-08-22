import { describe, expect, test } from "bun:test";

import { DragGesture, NO_MODIFIERS, SLOP_PIXELS, constrainToAxis, modifiersOf } from "./drag";

const at = (x: number, y: number) => ({ x, y });

describe("modifiersOf", () => {
    test("reads the three meanings", () => {
        expect(modifiersOf({ ctrlKey: true })).toEqual({ more: true, less: false, constrain: false });
        expect(modifiersOf({ altKey: true })).toEqual({ more: false, less: true, constrain: false });
        expect(modifiersOf({ shiftKey: true })).toEqual({ more: false, less: false, constrain: true });
    });

    test("Cmd counts as Ctrl, so macOS behaves", () => {
        expect(modifiersOf({ metaKey: true }).more).toBe(true);
    });

    test("a missing event holds nothing", () => {
        expect(modifiersOf(null)).toEqual(NO_MODIFIERS);
        expect(modifiersOf(undefined)).toEqual(NO_MODIFIERS);
    });
});

describe("DragGesture", () => {
    test("a press with no movement is not a drag", () => {
        const gesture = new DragGesture(at(100, 100));
        expect(gesture.active).toBe(false);
        expect(gesture.advance(at(100, 100))).toBe(false);
    });

    test("jitter under the slop does not start a drag", () => {
        const gesture = new DragGesture(at(100, 100));
        for (const point of [at(101, 100), at(100, 102), at(98, 99), at(102, 101)]) {
            expect(gesture.advance(point)).toBe(false);
        }
    });

    test("travelling the slop distance starts it", () => {
        const gesture = new DragGesture(at(100, 100));
        expect(gesture.advance(at(100 + SLOP_PIXELS, 100))).toBe(true);
        expect(gesture.active).toBe(true);
    });

    test("slop is radial, not per-axis", () => {
        const gesture = new DragGesture(at(0, 0), NO_MODIFIERS, 5);
        // 3-4-5: neither axis alone reaches 5, together they do.
        expect(gesture.advance(at(3, 4))).toBe(true);
    });

    test("a live drag stays live when the pointer wanders back", () => {
        // Otherwise a gesture would un-start itself mid-flight and drop the
        // geometry it was carrying.
        const gesture = new DragGesture(at(100, 100));
        gesture.advance(at(140, 140));
        expect(gesture.advance(at(100, 100))).toBe(true);
        expect(gesture.active).toBe(true);
    });

    test("a custom slop is honoured", () => {
        const gesture = new DragGesture(at(0, 0), NO_MODIFIERS, 20);
        expect(gesture.advance(at(10, 0))).toBe(false);
        expect(gesture.advance(at(20, 0))).toBe(true);
    });

    test("modifiers are latched at the press", () => {
        const gesture = new DragGesture(at(0, 0), modifiersOf({ altKey: true }));
        gesture.advance(at(50, 50));
        expect(gesture.modifiers).toEqual({ more: false, less: true, constrain: false });
    });

    test("latched modifiers cannot be changed by the caller afterwards", () => {
        // The whole point: one gesture must not pass through three modes and
        // finish in a state matching none of them.
        const held = { more: false, less: true, constrain: false };
        const gesture = new DragGesture(at(0, 0), held);
        held.less = false;
        held.more = true;
        expect(gesture.modifiers).toEqual({ more: false, less: true, constrain: false });
    });

    test("no modifiers by default", () => {
        expect(new DragGesture(at(0, 0)).modifiers).toEqual(NO_MODIFIERS);
    });
});

describe("constrainToAxis", () => {
    test("keeps the dominant axis and drops the other", () => {
        expect(constrainToAxis(at(10, 2), 0).x).toBeCloseTo(10, 6);
        expect(constrainToAxis(at(10, 2), 0).y).toBeCloseTo(0, 6);
    });

    test("keeps the cross axis when it dominates", () => {
        const result = constrainToAxis(at(2, 10), 0);
        expect(result.x).toBeCloseTo(0, 6);
        expect(result.y).toBeCloseTo(10, 6);
    });

    test("follows a rotated plan rather than the compass", () => {
        const axis = Math.PI / 4; // 45 degrees
        // Movement exactly along the rotated axis survives intact.
        const along = { x: Math.SQRT1_2 * 5, y: Math.SQRT1_2 * 5 };
        const result = constrainToAxis(along, axis);
        expect(result.x).toBeCloseTo(along.x, 6);
        expect(result.y).toBeCloseTo(along.y, 6);
    });

    test("movement across a rotated axis is projected onto the perpendicular", () => {
        const axis = Math.PI / 4;
        const across = { x: -Math.SQRT1_2 * 5, y: Math.SQRT1_2 * 5 };
        const result = constrainToAxis(across, axis);
        expect(result.x).toBeCloseTo(across.x, 6);
        expect(result.y).toBeCloseTo(across.y, 6);
    });

    test("a constrained delta is never longer than the original", () => {
        for (const delta of [at(3, 7), at(-9, 2), at(5, 5), at(-1, -8)]) {
            for (const axis of [0, 0.3, Math.PI / 6, Math.PI / 3]) {
                const result = constrainToAxis(delta, axis);
                expect(Math.hypot(result.x, result.y)).toBeLessThanOrEqual(Math.hypot(delta.x, delta.y) + 1e-9);
            }
        }
    });

    test("no movement constrains to no movement", () => {
        const result = constrainToAxis(at(0, 0), 0.7);
        expect(result.x).toBeCloseTo(0, 9);
        expect(result.y).toBeCloseTo(0, 9);
    });
});
