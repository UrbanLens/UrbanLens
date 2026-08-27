/**
 * Moving an opening from one wall to another.
 *
 * An opening is stored as a pair of fractions along the wall it belongs to, so
 * changing its host is not a matter of carrying two numbers across.
 */

import type { Pt } from "./coords";
import { type Opening, type OpeningKind, type OpeningSwing, type Wall, wallLength } from "./document";
import { clampOpening } from "./snapping";

/** Every swing, in the order they are offered, with what to call it. */
export const OPENING_SWINGS: ReadonlyArray<{ value: OpeningSwing; label: string }> = [
    { value: "none", label: "Not known" },
    { value: "left", label: "Hinged at the start" },
    { value: "right", label: "Hinged at the end" },
    { value: "double", label: "Double doors" },
];

/**
 * Whether a swing means anything for this kind of opening.
 *
 * A doorway is a door-shaped hole with no door in it, and a window or hatch has
 * nothing that sweeps across the floor. Offering the control anyway would be
 * asking a question with no answer.
 *
 * Args:
 *     kind: The opening's kind.
 *
 * Returns:
 *     True when the opening has a leaf that swings.
 */
export function swings(kind: OpeningKind): boolean {
    return kind === "door" || kind === "gate";
}

/**
 * The leaves of a door, as polylines to draw.
 *
 * Each leaf is a hinge point, a quarter-arc, and the leaf itself at its open
 * position - the standard plan symbol. A double door gets one from each end,
 * meeting in the middle.
 *
 * Which *side* of the wall a door opens into is not modelled: the schema's
 * choices are the architectural handing (left, right, double), and nothing
 * records inside-versus-outside. Everything is drawn to the wall's left, taking
 * a->b as forward. TODO: add a side to FloorplanOpening and honour it here;
 * until then a door on the wrong side is not something the editor can express.
 *
 * Args:
 *     wall: The wall the opening is cut into.
 *     opening: The opening.
 *     segments: Points per quarter-arc. Higher is smoother.
 *
 * Returns:
 *     One point list per leaf. Empty when nothing should be drawn.
 */
export function doorLeaves(wall: Wall, opening: Opening, segments = 8): Pt[][] {
    if (!swings(opening.kind) || opening.swing === "none") return [];
    const length = wallLength(wall);
    if (length < 1e-6) return [];
    const forward = { x: (wall.bx - wall.ax) / length, y: (wall.by - wall.ay) / length };
    // The wall's left, taking a->b as forward.
    const side = { x: -forward.y, y: forward.x };
    const at = (t: number): Pt => ({ x: wall.ax + (wall.bx - wall.ax) * t, y: wall.ay + (wall.by - wall.ay) * t });

    const width = (opening.t_end - opening.t_start) * length;
    const hinges: Array<{ point: Pt; reach: number; span: number }> =
        opening.swing === "double"
            ? [
                  { point: at(opening.t_start), reach: width / 2, span: 1 },
                  { point: at(opening.t_end), reach: width / 2, span: -1 },
              ]
            : opening.swing === "left"
              ? [{ point: at(opening.t_start), reach: width, span: 1 }]
              : [{ point: at(opening.t_end), reach: width, span: -1 }];

    return hinges.map(({ point, reach, span }) => {
        const leaf: Pt[] = [];
        for (let step = 0; step <= segments; step++) {
            // From flat along the wall round to square with it.
            const angle = (Math.PI / 2) * (step / segments);
            const alongPart = Math.cos(angle) * reach * span;
            const sidePart = Math.sin(angle) * reach;
            leaf.push({ x: point.x + forward.x * alongPart + side.x * sidePart, y: point.y + forward.y * alongPart + side.y * sidePart });
        }
        // The open leaf itself, back to its hinge, so the symbol reads as a
        // door and not as a stray arc.
        leaf.push(point);
        return leaf;
    });
}

/**
 * Move an opening onto a different wall, keeping the width it was given.
 *
 * The fractions would make a door narrower on a long wall and wider on a short
 * one. The metre width is what the author actually chose, so that is what
 * survives the move - clamped to the new wall when the door is wider than the
 * wall it is being put in.
 *
 * Args:
 *     opening: The opening to move. Mutated in place.
 *     from: The wall it currently belongs to.
 *     to: The wall it should belong to. May be the same wall.
 *     centreMeters: Where its middle should sit along the new wall, measured
 *         from that wall's own start. Clamped so the opening stays inside.
 *
 * Returns:
 *     True when the move happened. False for a target too short to hold
 *     anything, which leaves both walls untouched.
 */
export function rehostOpening(opening: Opening, from: Wall, to: Wall, centreMeters: number): boolean {
    const length = wallLength(to);
    if (length < 1e-6) return false;
    const widthMeters = (opening.t_end - opening.t_start) * wallLength(from);
    from.openings = from.openings.filter((item) => item !== opening);
    const halfWidth = Math.min(widthMeters, length) / 2;
    const centre = Math.min(Math.max(centreMeters, halfWidth), length - halfWidth);
    const [start, end] = clampOpening((centre - halfWidth) / length, (centre + halfWidth) / length);
    opening.t_start = start;
    opening.t_end = end;
    // Same wall: `from.openings` was reassigned above, and `to` is that same
    // object, so this pushes onto the filtered array rather than the old one.
    to.openings.push(opening);
    return true;
}
