/**
 * Where a drawn point actually lands.
 *
 * Drawing a building from memory is imprecise, so the editor corrects the
 * pointer toward what the author plainly meant - an existing corner, a wall
 * they are joining, a right angle. Two rules keep that from becoming a fight:
 *
 * - **Exactly one snap wins.** Blending two candidates produces a point that
 *   satisfies neither and lands somewhere nobody aimed at.
 * - **Tolerances are screen pixels, converted to metres by the caller.** A
 *   fixed metre tolerance is unusable at both zoom extremes: it is invisible
 *   when zoomed out and it swallows whole rooms when zoomed in.
 *
 * Snapping here is *destructive* - the returned coordinate is what gets
 * stored - which is why it is deliberately conservative. Forgiving the gaps
 * that survive is `planar.ts`'s job, and that stage never moves what the
 * author drew.
 */

import { type Pt, add, angleOf, distance, projectOnSegment, rotate, scale, sub } from "./coords";
import type { Segment } from "./planar";

/** Which rule placed a point, for the on-screen readout. */
export type SnapKind = "free" | "endpoint" | "wall" | "extension" | "angle" | "length" | "grid";

export interface Snap {
    point: Pt;
    kind: SnapKind;
    /** Human-facing name of the rule, shown beside the cursor. */
    label: string;
}

/**
 * Tolerances in metres. Callers derive these from screen pixels by dividing by
 * the current metres-per-pixel scale, so the felt size stays constant at any
 * zoom.
 */
export interface Tolerances {
    endpoint: number;
    wall: number;
    extension: number;
}

/** Screen-pixel tolerances, in priority order. Divided by zoom by the caller. */
export const PIXEL_TOLERANCES = { endpoint: 12, wall: 8, extension: 8 } as const;

/** Angle increment for angle snapping, and how close counts as "on" it. */
export const ANGLE_STEP_RADIANS = Math.PI / 4;
export const ANGLE_CAPTURE_RADIANS = (7 * Math.PI) / 180;

/** Lengths round to this many metres once an angle snap has taken. */
export const LENGTH_STEP_METERS = 0.25;

/**
 * Grid line spacing, and the grid-snap step - the same value as
 * LENGTH_STEP_METERS, deliberately: a drawn grid, a length rounded by an
 * angle snap, and a point snapped to the grid should all agree on what "one
 * unit" means, rather than introduce a second, different notion of it.
 */
export const GRID_SPACING_METERS = LENGTH_STEP_METERS;

/**
 * Once a snap has taken, the pointer must travel this multiple of the
 * tolerance to break it. Without the margin a cursor sitting near the boundary
 * flickers between snapped and free every frame.
 */
export const HYSTERESIS = 1.5;

const free = (point: Pt): Snap => ({ point, kind: "free", label: "" });

/**
 * Snap *cursor* against existing geometry and, failing that, against the
 * drawing axis.
 *
 * Args:
 *     cursor: The raw pointer position, in plan-local metres.
 *     segments: Walls already on this floor.
 *     tolerances: Metre tolerances, converted from pixels by the caller.
 *     from: The previous point of a chain being drawn, when one is in
 *         progress - angle and length snapping are relative to it.
 *     axisRadians: The plan's drawing axis, so right angles land square
 *         against a building that does not face north.
 *     suspended: True while the author holds the suspend key; returns the
 *         cursor untouched so an intentional off-grid point is possible.
 *     grid: Spacing and tolerance to snap to the visible grid, or null when
 *         the grid is off. Tried last, since it is - like the angle snap -
 *         a fallback for "no real geometry nearby," and only one fallback
 *         should win; unlike the angle snap it needs no previous point, so
 *         it can also catch the very first point of a new chain.
 *
 * Returns:
 *     Where the point lands and which rule put it there.
 */
export function snapPoint(
    cursor: Pt,
    segments: readonly Segment[],
    tolerances: Tolerances,
    {
        from = null,
        axisRadians = 0,
        suspended = false,
        grid = null,
    }: { from?: Pt | null; axisRadians?: number; suspended?: boolean; grid?: { spacing: number; tolerance: number } | null } = {},
): Snap {
    if (suspended) return free(cursor);

    // 1. An existing endpoint. Highest priority: joining a corner exactly is
    //    almost always the intent, and it is what makes a region close.
    let best: { point: Pt; d: number } | null = null;
    for (const segment of segments) {
        for (const end of [segment.a, segment.b]) {
            const d = distance(cursor, end);
            if (d <= tolerances.endpoint && (!best || d < best.d)) best = { point: end, d };
        }
    }
    if (best) return { point: best.point, kind: "endpoint", label: "endpoint" };

    // 2. A point on a wall's interior - how a partition meets an exterior
    //    wall. Without this the two never share a vertex and no room closes.
    let onWall: { point: Pt; d: number } | null = null;
    for (const segment of segments) {
        const near = projectOnSegment(cursor, segment.a, segment.b);
        if (near.t <= 0 || near.t >= 1) continue;
        if (near.distance <= tolerances.wall && (!onWall || near.distance < onWall.d)) {
            onWall = { point: near.point, d: near.distance };
        }
    }
    if (onWall) return { point: onWall.point, kind: "wall", label: "on wall" };

    // 3. The continuation of an existing wall past its end - the line someone
    //    is visually aligning to even though nothing is drawn there.
    const extension = snapToExtension(cursor, segments, tolerances.extension);
    if (extension) return { point: extension, kind: "extension", label: "extension" };

    // 4. Failing all of those, square the segment being drawn to the axis.
    if (from) {
        const angled = snapToAngle(from, cursor, axisRadians);
        if (angled) return angled;
    }

    // 5. Still nothing - the grid, if it's on.
    if (grid) {
        const gridded = snapToGrid(cursor, axisRadians, grid.spacing, grid.tolerance);
        if (gridded) return gridded;
    }
    return free(cursor);
}

/**
 * The nearest grid intersection, in the plan's own (possibly rotated) axis.
 *
 * Rotating into axis-space before rounding, then back, is what keeps the
 * grid square to the drawing axis rather than to true north - the same
 * reasoning `snapToAngle` uses for right angles.
 */
function snapToGrid(cursor: Pt, axisRadians: number, spacing: number, tolerance: number): Snap | null {
    const local = rotate(cursor, -axisRadians);
    const nearest = { x: Math.round(local.x / spacing) * spacing, y: Math.round(local.y / spacing) * spacing };
    if (distance(local, nearest) > tolerance) return null;
    return { point: rotate(nearest, axisRadians), kind: "grid", label: "grid" };
}

/**
 * The nearest point on any wall's infinite extension, beyond its endpoints.
 *
 * Only the region past an end counts - inside the segment is the wall snap
 * above, and reporting it here would mean two rules claiming one point.
 */
function snapToExtension(cursor: Pt, segments: readonly Segment[], tolerance: number): Pt | null {
    let best: { point: Pt; d: number } | null = null;
    for (const segment of segments) {
        const direction = sub(segment.b, segment.a);
        const lengthSquared = direction.x * direction.x + direction.y * direction.y;
        if (lengthSquared === 0) continue;
        const t = ((cursor.x - segment.a.x) * direction.x + (cursor.y - segment.a.y) * direction.y) / lengthSquared;
        if (t > 0 && t < 1) continue; // on the segment itself
        const projected = add(segment.a, scale(direction, t));
        const d = distance(cursor, projected);
        if (d <= tolerance && (!best || d < best.d)) best = { point: projected, d };
    }
    return best ? best.point : null;
}

/**
 * Square the segment ``from``->``cursor`` onto the nearest axis increment.
 *
 * Returns null when the cursor is not close enough to an increment, so a
 * deliberately skewed wall stays skewed.
 */
function snapToAngle(from: Pt, cursor: Pt, axisRadians: number): Snap | null {
    const raw = angleOf(from, cursor) - axisRadians;
    const stepped = Math.round(raw / ANGLE_STEP_RADIANS) * ANGLE_STEP_RADIANS;
    let delta = raw - stepped;
    // Normalize into [-pi, pi] so the comparison is a real angular distance
    // rather than one that wraps at the +/-pi boundary.
    while (delta > Math.PI) delta -= 2 * Math.PI;
    while (delta < -Math.PI) delta += 2 * Math.PI;
    if (Math.abs(delta) > ANGLE_CAPTURE_RADIANS) return null;

    const rounded = Math.round(distance(from, cursor) / LENGTH_STEP_METERS) * LENGTH_STEP_METERS;
    const length = rounded > 0 ? rounded : distance(from, cursor);
    const angle = stepped + axisRadians;
    const degrees = Math.round(((stepped * 180) / Math.PI + 360) % 360);
    return {
        point: { x: from.x + Math.cos(angle) * length, y: from.y + Math.sin(angle) * length },
        kind: "angle",
        label: `${degrees}° · ${length.toFixed(2)} m`,
    };
}

/**
 * Whether a snap that has already taken should be released.
 *
 * Args:
 *     held: The point the cursor is currently snapped to.
 *     cursor: The raw pointer position.
 *     tolerance: The tolerance that produced the snap.
 *
 * Returns:
 *     True once the pointer has moved far enough that holding on would feel
 *     sticky rather than helpful.
 */
export function shouldRelease(held: Pt, cursor: Pt, tolerance: number): boolean {
    return distance(held, cursor) > tolerance * HYSTERESIS;
}

/**
 * Convert screen-pixel tolerances into metres at the current zoom.
 *
 * Args:
 *     metersPerPixel: How many metres one screen pixel covers right now.
 *
 * Returns:
 *     The same tolerances expressed in plan-local metres.
 */
export function tolerancesFor(metersPerPixel: number): Tolerances {
    return {
        endpoint: PIXEL_TOLERANCES.endpoint * metersPerPixel,
        wall: PIXEL_TOLERANCES.wall * metersPerPixel,
        extension: PIXEL_TOLERANCES.extension * metersPerPixel,
    };
}

/**
 * Where along a wall a point sits, as the parameter an opening is stored by.
 *
 * Args:
 *     point: A position near the wall, in plan-local metres.
 *     wall: The wall being measured along.
 *
 * Returns:
 *     The parameter in [0, 1] from the wall's ``a`` end to its ``b`` end.
 */
export function parameterAlong(point: Pt, wall: { a: Pt; b: Pt }): number {
    return projectOnSegment(point, wall.a, wall.b).t;
}

/**
 * Clamp an opening's interval so it stays inside its wall and keeps its order.
 *
 * The database rejects anything outside ``0 <= t_start < t_end <= 1``, so a
 * drag that would produce one has to be corrected before it is sent rather
 * than rejected after.
 *
 * Args:
 *     start: Proposed start parameter.
 *     end: Proposed end parameter.
 *     minimum: Smallest interval to allow, so an opening cannot collapse to
 *         nothing and vanish.
 *
 * Returns:
 *     A valid ``[start, end]`` pair.
 */
export function clampOpening(start: number, end: number, minimum = 0.02): [number, number] {
    const low = Math.min(Math.max(Math.min(start, end), 0), 1 - minimum);
    const high = Math.max(Math.min(Math.max(start, end), 1), low + minimum);
    return [low, Math.min(high, 1)];
}

/**
 * Nudge a rigid translation so one of the points it carries lands on a snap.
 *
 * A group being dragged - a whole wall, a whole room - has to move by one
 * delta or it stops being the shape it was. So rather than snapping each point
 * independently, this finds whichever carried point comes nearest a target and
 * corrects the shared delta by that much.
 *
 * The caller is responsible for leaving out every wall the drag itself
 * rewrites. A wall being stretched to follow the thing under the cursor has
 * its corner moved every frame, so leaving it in makes the drag snap to the
 * position it produced a frame ago, and stop moving.
 *
 * Args:
 *     moved: The carried points, at their pre-drag positions.
 *     delta: The translation asked for, in plan-local metres.
 *     segments: Candidate geometry, already filtered by the caller.
 *     tolerances: Metre tolerances for this zoom.
 *
 * Returns:
 *     The delta to apply - corrected toward the nearest snap within reach, or
 *     unchanged when nothing is close enough.
 */
export function snapTranslation(moved: readonly Pt[], delta: Pt, segments: readonly Segment[], tolerances: Tolerances): Pt {
    if (!moved.length || !segments.length) return delta;
    let best: { dx: number; dy: number; away: number } | null = null;
    for (const point of moved) {
        const target = { x: point.x + delta.x, y: point.y + delta.y };
        const snapped = snapPoint(target, segments, tolerances);
        if (snapped.kind === "free") continue;
        const dx = snapped.point.x - target.x;
        const dy = snapped.point.y - target.y;
        const away = Math.hypot(dx, dy);
        if (!best || away < best.away) best = { dx, dy, away };
    }
    return best ? { x: delta.x + best.dx, y: delta.y + best.dy } : delta;
}
