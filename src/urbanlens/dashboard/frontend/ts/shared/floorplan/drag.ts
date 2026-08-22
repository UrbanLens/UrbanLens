/**
 * The part of a drag that is the same everywhere.
 *
 * The editor grew five hand-rolled drag implementations - room fill, wall body,
 * opening, opening end, wall corner - which disagreed about how far the pointer
 * must travel before a press counts as a drag, about whether the gesture is
 * undoable, and about whether a modifier read halfway through changes what is
 * already happening. That last one is the sharpest: reading `altKey` fresh on
 * every move lets one gesture pass through three different modes and finish in
 * a state matching none of them.
 *
 * So a mode is *latched* when the gesture starts and frozen for its duration,
 * while position keeps updating - because a mode decides which geometry moves,
 * and moving the goalposts mid-flight is what produced geometry nobody asked
 * for. Snapping is deliberately not latched: it only decides where the current
 * frame lands, and every frame recomputes from the captured origin anyway.
 */

import { type Pt, distance } from "./coords";

/**
 * What the modifier keys mean, in the one vocabulary the editor uses.
 *
 * Each is a momentary accelerator for a control that is also visible, which is
 * what keeps the whole model usable without a keyboard.
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
 *
 * Args:
 *     event: The originating event.
 *
 * Returns:
 *     The three meanings, with Cmd treated as Ctrl so macOS behaves.
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
 * How far the pointer must travel, in screen pixels, before a press becomes a
 * drag rather than a click.
 *
 * One number for every gesture: the editor previously used 4 in three places
 * and none at all in two, so grabbing a wall corner moved it by a pixel when
 * the user meant to select it.
 */
export const SLOP_PIXELS = 4;

/**
 * A press that may or may not become a drag.
 *
 * Track it with :meth:`advance` on every pointer move; it reports whether the
 * gesture is live yet, so the caller can defer taking an undo checkpoint and
 * disabling the map's own panning until the user has actually committed to
 * dragging something.
 */
export class DragGesture {
    private live = false;

    private readonly latched: DragModifiers;

    /**
     * Args:
     *     origin: Where the press landed, in screen pixels.
     *     modifiers: The keyboard state at the press, frozen for the gesture.
     *     slop: How far to travel before this counts as a drag.
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
     *
     * Args:
     *     point: The current pointer position, in screen pixels.
     *
     * Returns:
     *     Whether the gesture is live. Once it is, it stays live even if the
     *     pointer wanders back inside the slop radius - a drag that
     *     un-started itself halfway would drop the geometry it was carrying.
     */
    advance(point: Pt): boolean {
        if (!this.live && distance(this.origin, point) >= this.slop) this.live = true;
        return this.live;
    }
}

/**
 * Square a translation onto the plan's axis.
 *
 * Used for the "constrain" modifier: a wall being nudged into line with its
 * neighbours usually wants to move along one axis only, and free movement in
 * both makes that impossible to hit by hand.
 *
 * Args:
 *     delta: The unconstrained movement, in plan-local metres.
 *     axisRadians: The plan's drawing axis, so a building that does not face
 *         north constrains to its own walls rather than to the compass.
 *
 * Returns:
 *     The movement projected onto whichever axis direction it is closest to.
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
