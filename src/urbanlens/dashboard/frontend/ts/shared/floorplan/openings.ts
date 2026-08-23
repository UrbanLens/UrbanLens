/**
 * Moving an opening from one wall to another.
 *
 * An opening is stored as a pair of fractions along the wall it belongs to, so
 * changing its host is not a matter of carrying two numbers across.
 */

import { type Opening, type Wall, wallLength } from "./document";
import { clampOpening } from "./snapping";

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
