/**
 * The part of a drag that is the same everywhere.
 */

import { type Pt, distance } from "./coords";

/**
 * What the modifier keys mean, in the one vocabulary the editor uses.
 */
export interface DragModifiers {
    /** Ctrl / Cmd - take more: extend a selection, carry the whole network. */
    more: boolean;
    /** Alt / Option - take less: detach from neighbours, stay on this wall. */
    less: boolean;
    /** Shift - constrain: lock to the plan's axis. */
    constrain: boolean;
}

/** The keyboard state an event carries, however it spells it. */
export interface ModifierSource {
    ctrlKey?: boolean;
    metaKey?: boolean;
    altKey?: boolean;
    shiftKey?: boolean;
}

/**
 * Read the modifier vocabulary off a pointer or mouse event.
 */
export function modifiersOf(event: ModifierSource | null | undefined): DragModifiers {
    return {
        more: Boolean(event?.ctrlKey || event?.metaKey),
        less: Boolean(event?.altKey),
        constrain: Boolean(event?.shiftKey),
    };
}

/** No modifiers held. */
export const NO_MODIFIERS: DragModifiers = { more: false, less: false, constrain: false };

/**
 * How far the pointer must travel, in screen pixels, before a press becomes a drag rather than a click.
 */
export const SLOP_PIXELS = 4;

/**
 * A press that may or may not become a drag.
 */
export class DragGesture {
    private live = false;

    private readonly latched: DragModifiers;

    /**
 * Args: origin: Where the press landed, in screen pixels. modifiers: The keyboard state at the press, frozen for the gesture. slop.
 */
    constructor(
        private readonly origin: Pt,
        modifiers: DragModifiers = NO_MODIFIERS,
        private readonly slop: number = SLOP_PIXELS,
    ) {
        this.latched = { ...modifiers };
    }

    /** The keyboard state as it was when the press landed. */
    get modifiers(): DragModifiers {
        return this.latched;
    }

    /** Whether the pointer has committed to a drag. */
    get active(): boolean {
        return this.live;
    }

    /**
 * Record a pointer position.
 */
    advance(point: Pt): boolean {
        if (!this.live && distance(this.origin, point) >= this.slop) this.live = true;
        return this.live;
    }
}

/**
 * Square a translation onto the plan's axis.
 */
export function constrainToAxis(delta: Pt, axisRadians: number): Pt {
    const cos = Math.cos(axisRadians);
    const sin = Math.sin(axisRadians);
    // Rotate into the plan's frame, drop the smaller component, rotate back.
    const along = delta.x * cos + delta.y * sin;
    const across = -delta.x * sin + delta.y * cos;
    if (Math.abs(along) >= Math.abs(across)) {
        return { x: along * cos, y: along * sin };
    }
    return { x: -across * sin, y: across * cos };
}

/** Rotation snaps to this many degrees unless the author suspends snapping. */
export const ROTATION_STEP_DEGREES = 15;

/**
 * Round a rotation onto the nearest step.
 */
export function snapRotation(radians: number, stepDegrees: number = ROTATION_STEP_DEGREES): number {
    if (stepDegrees <= 0) return radians;
    const step = (stepDegrees * Math.PI) / 180;
    // The + 0 collapses negative zero, which rounding a small negative angle
    // produces and which formats as "-0°" in a readout.
    return Math.round(radians / step) * step + 0;
}
