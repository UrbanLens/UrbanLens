/**
 * Where a drawn point actually lands.
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
 * Grid line spacing, and the grid-snap step - the same value as LENGTH_STEP_METERS, deliberately.
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
 * Snap *cursor* against existing geometry and, failing that, against the drawing axis.
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
 */
function snapToGrid(cursor: Pt, axisRadians: number, spacing: number, tolerance: number): Snap | null {
    const local = rotate(cursor, -axisRadians);
    const nearest = { x: Math.round(local.x / spacing) * spacing, y: Math.round(local.y / spacing) * spacing };
    if (distance(local, nearest) > tolerance) return null;
    return { point: rotate(nearest, axisRadians), kind: "grid", label: "grid" };
}

/**
 * The nearest point on any wall's infinite extension, beyond its endpoints.
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
 */
export function shouldRelease(held: Pt, cursor: Pt, tolerance: number): boolean {
    return distance(held, cursor) > tolerance * HYSTERESIS;
}

/**
 * Convert screen-pixel tolerances into metres at the current zoom.
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
 */
export function parameterAlong(point: Pt, wall: { a: Pt; b: Pt }): number {
    return projectOnSegment(point, wall.a, wall.b).t;
}

/**
 * Clamp an opening's interval so it stays inside its wall and keeps its order.
 */
export function clampOpening(start: number, end: number, minimum = 0.02): [number, number] {
    const low = Math.min(Math.max(Math.min(start, end), 0), 1 - minimum);
    const high = Math.max(Math.min(Math.max(start, end), 1), low + minimum);
    return [low, Math.min(high, 1)];
}

/**
 * Nudge a rigid translation so one of the points it carries lands on a snap.
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
