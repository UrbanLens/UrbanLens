/**
 * Moving an opening from one wall to another.
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
 */
export function swings(kind: OpeningKind): boolean {
    return kind === "door" || kind === "gate";
}

/**
 * The leaves of a door, as polylines to draw.
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
