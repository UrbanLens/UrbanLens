/**
 * Geometry edits on snapshot shapes: moving, scaling and turning them, and adding, moving and
 * removing their points.
 *
 * The arithmetic happens in projected pixels at one zoom, not in degrees - a degree of longitude is
 * not a degree of latitude, so scaling or turning in degrees skews the shape. The caller supplies
 * the projection (the map's own `project`/`unproject` at its current zoom), which also keeps the
 * module free of Leaflet. Projected pixels do not turn with the map, so none of this changes when
 * the view does.
 */
import { textRotation, type LatLngTuple, type ShapeSpec } from "./markup-engine";
import { cloneShape } from "./markup-document";

export interface Pt {
    x: number;
    y: number;
}

export interface Projection {
    project: (ll: LatLngTuple) => Pt;
    unproject: (point: Pt) => LatLngTuple;
}

export interface Box {
    minX: number;
    minY: number;
    maxX: number;
    maxY: number;
}

/** Fewest points a shape whose points can be removed keeps. */
const MIN_VERTICES: Partial<Record<ShapeSpec["type"], number>> = { line: 2, arrow: 2, polygon: 3 };

/** Text sizes the renderer accepts (`textLabelHtml` clamps to the same). */
export const TEXT_SIZE_MIN = 8;
export const TEXT_SIZE_MAX = 96;

/** Whether this type is drawn from a list of points that can be added to and removed from. */
export function hasEditableVertices(shape: ShapeSpec): boolean {
    return shape.type === "line" || shape.type === "arrow" || shape.type === "polygon";
}

export function canRemoveVertex(shape: ShapeSpec): boolean {
    const min = MIN_VERTICES[shape.type];
    return min !== undefined && shape.latlngs.length > min;
}

/** Whether the scale handle applies: a pin is a fixed-size icon. */
export function canScale(shape: ShapeSpec): boolean {
    return shape.type !== "pin";
}

/** Whether the turn handle applies: a circle looks the same at every turn, and a pin stays upright. */
export function canRotate(shape: ShapeSpec): boolean {
    return shape.type !== "circle" && shape.type !== "pin";
}

/** The four corners of a rectangle, starting at the first point it was drawn from and going round. */
export function rectCorners(shape: ShapeSpec): LatLngTuple[] {
    const [a, b] = shape.latlngs as [LatLngTuple, LatLngTuple];
    return [
        [a[0], a[1]],
        [a[0], b[1]],
        [b[0], b[1]],
        [b[0], a[1]],
    ];
}

/**
 * The points a viewer can grab: each vertex of a line or polygon, a rectangle's four corners, a
 * circle's centre and the point on its edge, and a label's or pin's anchor.
 */
export function editablePoints(shape: ShapeSpec): LatLngTuple[] {
    if (shape.type === "rect") return rectCorners(shape);
    if (shape.type === "text" || shape.type === "pin") return [shape.latlngs[0]!];
    return shape.latlngs.map((ll) => [ll[0], ll[1]] as LatLngTuple);
}

function mapPoints(shape: ShapeSpec, fn: (point: Pt) => Pt, proj: Projection): ShapeSpec {
    const next = cloneShape(shape);
    next.latlngs = shape.latlngs.map((ll) => proj.unproject(fn(proj.project(ll))));
    return next;
}

/** The shape moved by `dx`, `dy` projected pixels. */
export function translateShape(shape: ShapeSpec, dx: number, dy: number, proj: Projection): ShapeSpec {
    return mapPoints(shape, (p) => ({ x: p.x + dx, y: p.y + dy }), proj);
}

/**
 * The shape scaled by `factor` about `origin`. A label's text grows with it; a pin is an icon of
 * one size, so it stays as it is.
 */
export function scaleShape(shape: ShapeSpec, factor: number, origin: Pt, proj: Projection): ShapeSpec {
    if (shape.type === "pin" || !Number.isFinite(factor) || factor <= 0) return cloneShape(shape);
    if (shape.type === "text") {
        const next = cloneShape(shape);
        const size = Math.round(Math.max(TEXT_SIZE_MIN, Math.min(TEXT_SIZE_MAX, (shape.stroke_width ?? 16) * factor)));
        next.stroke_width = size;
        // Only the box corner moves with the scale: the anchor is where the label was put.
        if (shape.latlngs.length > 1) {
            const anchor = proj.project(shape.latlngs[0]!);
            const corner = proj.project(shape.latlngs[1]!);
            const ratio = size / (shape.stroke_width ?? 16);
            next.latlngs = [shape.latlngs[0]!, proj.unproject({ x: anchor.x + (corner.x - anchor.x) * ratio, y: anchor.y + (corner.y - anchor.y) * ratio })];
        }
        return next;
    }
    return mapPoints(shape, (p) => ({ x: origin.x + (p.x - origin.x) * factor, y: origin.y + (p.y - origin.y) * factor }), proj);
}

/**
 * The shape turned `degrees` clockwise about `origin`.
 *
 * A rectangle is stored by two opposite corners and is always square to north, so turning one makes
 * it a four-sided polygon. A label keeps its anchor and turns about its own middle. A circle and a
 * pin look the same however they are turned.
 */
export function rotateShape(shape: ShapeSpec, degrees: number, origin: Pt, proj: Projection): ShapeSpec {
    if (!Number.isFinite(degrees) || !degrees) return cloneShape(shape);
    if (shape.type === "text") {
        const next = cloneShape(shape);
        const turned = textRotation({ rotation: (shape.rotation ?? 0) + degrees });
        if (turned) next.rotation = turned;
        else delete next.rotation;
        return next;
    }
    if (shape.type === "circle" || shape.type === "pin") return cloneShape(shape);
    const radians = (degrees * Math.PI) / 180;
    const cos = Math.cos(radians);
    const sin = Math.sin(radians);
    // Screen y grows downwards, so this is clockwise as the viewer sees it.
    const turn = (p: Pt): Pt => ({ x: origin.x + (p.x - origin.x) * cos - (p.y - origin.y) * sin, y: origin.y + (p.x - origin.x) * sin + (p.y - origin.y) * cos });
    const base = shape.type === "rect" ? { ...cloneShape(shape), type: "polygon" as const, latlngs: rectCorners(shape) } : shape;
    return mapPoints(base, turn, proj);
}

/**
 * The shape with one of its {@link editablePoints} moved to `ll`.
 *
 * A rectangle keeps the corner opposite fixed. Moving a circle's centre carries the circle; moving
 * the point on its edge changes its radius. A label or pin moves whole.
 */
export function moveEditablePoint(shape: ShapeSpec, index: number, ll: LatLngTuple, proj: Projection): ShapeSpec {
    const next = cloneShape(shape);
    if (shape.type === "rect") {
        const corners = rectCorners(shape);
        const opposite = corners[(index + 2) % 4];
        if (!opposite || !corners[index]) return next;
        next.latlngs = [opposite, ll];
        return next;
    }
    if (shape.type === "text" || shape.type === "pin" || (shape.type === "circle" && index === 0)) {
        const from = proj.project(shape.latlngs[0]!);
        const to = proj.project(ll);
        return translateShape(shape, to.x - from.x, to.y - from.y, proj);
    }
    if (index < 0 || index >= next.latlngs.length) return next;
    next.latlngs[index] = [ll[0], ll[1]];
    return next;
}

/** Inserts a vertex before `index` (after the last one when it equals the count). */
export function insertVertex(shape: ShapeSpec, index: number, ll: LatLngTuple): ShapeSpec {
    const next = cloneShape(shape);
    if (!hasEditableVertices(shape)) return next;
    const at = Math.max(0, Math.min(next.latlngs.length, index));
    next.latlngs.splice(at, 0, [ll[0], ll[1]]);
    return next;
}

/** The shape without vertex `index`, or null when that would leave too few to draw it. */
export function removeVertex(shape: ShapeSpec, index: number): ShapeSpec | null {
    if (!canRemoveVertex(shape) || index < 0 || index >= shape.latlngs.length) return null;
    const next = cloneShape(shape);
    next.latlngs.splice(index, 1);
    return next;
}

/** A new vertex goes in the middle of each edge; `index` is where {@link insertVertex} puts it. */
export function edgeMidpoints(shape: ShapeSpec, proj: Projection): Array<{ index: number; ll: LatLngTuple }> {
    if (!hasEditableVertices(shape)) return [];
    const points = shape.latlngs;
    const edges = shape.type === "polygon" ? points.length : points.length - 1;
    const result: Array<{ index: number; ll: LatLngTuple }> = [];
    for (let i = 0; i < edges; i++) {
        const a = proj.project(points[i]!);
        const b = proj.project(points[(i + 1) % points.length]!);
        result.push({ index: i + 1, ll: proj.unproject({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }) });
    }
    return result;
}

/**
 * Where a line or arrow can be pulled longer from: `offset` pixels beyond each end, along its last
 * segment. Null for any other type.
 */
export function extendPoints(shape: ShapeSpec, proj: Projection, offset: number): { start: LatLngTuple; end: LatLngTuple } | null {
    if (shape.type !== "line" && shape.type !== "arrow") return null;
    const n = shape.latlngs.length;
    const beyond = (from: LatLngTuple, to: LatLngTuple): LatLngTuple => {
        const a = proj.project(from);
        const b = proj.project(to);
        const length = Math.hypot(b.x - a.x, b.y - a.y) || 1;
        return proj.unproject({ x: b.x + ((b.x - a.x) / length) * offset, y: b.y + ((b.y - a.y) / length) * offset });
    };
    return { start: beyond(shape.latlngs[1]!, shape.latlngs[0]!), end: beyond(shape.latlngs[n - 2]!, shape.latlngs[n - 1]!) };
}

/**
 * The projected box around a shape.
 * @param labelSize - A label's or pin's drawn size in pixels; the shape alone does not say how big its text renders.
 */
export function shapeBox(shape: ShapeSpec, proj: Projection, labelSize?: { width: number; height: number }): Box {
    if (shape.type === "circle") {
        const c = proj.project(shape.latlngs[0]!);
        const e = proj.project(shape.latlngs[1]!);
        const r = Math.hypot(e.x - c.x, e.y - c.y);
        return { minX: c.x - r, minY: c.y - r, maxX: c.x + r, maxY: c.y + r };
    }
    if (shape.type === "text") {
        const a = proj.project(shape.latlngs[0]!);
        const w = labelSize?.width ?? 0;
        const h = labelSize?.height ?? 0;
        return { minX: a.x, minY: a.y, maxX: a.x + w, maxY: a.y + h };
    }
    if (shape.type === "pin") {
        const a = proj.project(shape.latlngs[0]!);
        const w = labelSize?.width ?? 32;
        const h = labelSize?.height ?? 32;
        return { minX: a.x - w / 2, minY: a.y - h, maxX: a.x + w / 2, maxY: a.y };
    }
    const points = (shape.type === "rect" ? rectCorners(shape) : shape.latlngs).map((ll) => proj.project(ll));
    return {
        minX: Math.min(...points.map((p) => p.x)),
        minY: Math.min(...points.map((p) => p.y)),
        maxX: Math.max(...points.map((p) => p.x)),
        maxY: Math.max(...points.map((p) => p.y)),
    };
}

export function boxCenter(box: Box): Pt {
    return { x: (box.minX + box.maxX) / 2, y: (box.minY + box.maxY) / 2 };
}

/** Clockwise angle in degrees from `origin` to `point`, 0 pointing up. */
export function angleFrom(origin: Pt, point: Pt): number {
    return (Math.atan2(point.x - origin.x, origin.y - point.y) * 180) / Math.PI;
}
