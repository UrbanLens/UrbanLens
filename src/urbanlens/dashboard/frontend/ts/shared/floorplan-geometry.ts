/**
 * Vertex arithmetic for the floorplan editor's shapes.
 *
 * Split out of the editor entry because this is where the fiddly rules live -
 * a GeoJSON polygon repeats its first point last, so editing "the vertices"
 * means editing one fewer than the ring holds and mirroring index 0 onto the
 * repeat; and a shape has a minimum below which it stops being that shape.
 * Both are easy to get subtly wrong and easy to test directly.
 */

export type Position = [number, number];
export type EditableGeometry = { type: string; coordinates: unknown } | null;

/** The editable coordinate ring of a geometry, or null when it has none. */
export function ringOf(geometry: EditableGeometry): Position[] | null {
    if (!geometry) return null;
    if (geometry.type === "Polygon") return (geometry.coordinates as Position[][])[0] || null;
    if (geometry.type === "LineString") return geometry.coordinates as Position[];
    return null;
}

/** Whether this geometry's ring repeats its first point last. */
export function isClosedRing(geometry: EditableGeometry): boolean {
    return !!geometry && geometry.type === "Polygon";
}

/** How many *distinct* vertices a geometry has (the repeat doesn't count). */
export function vertexCount(geometry: EditableGeometry): number {
    const ring = ringOf(geometry);
    if (!ring) return 0;
    return isClosedRing(geometry) ? Math.max(0, ring.length - 1) : ring.length;
}

/** The fewest distinct vertices this shape can have and still be one. */
export function minimumVertices(geometry: EditableGeometry): number {
    return isClosedRing(geometry) ? 3 : 2;
}

/** Two positions close enough to be the same click. */
export function samePoint(a: Position, b: Position, tolerance = 1e-9): boolean {
    return Math.abs(a[0] - b[0]) < tolerance && Math.abs(a[1] - b[1]) < tolerance;
}

/**
 * Move one vertex, keeping a closed ring closed.
 *
 * @returns true when the geometry changed.
 */
export function moveVertex(geometry: EditableGeometry, index: number, point: Position): boolean {
    const ring = ringOf(geometry);
    if (!ring || index < 0 || index >= vertexCount(geometry)) return false;
    ring[index] = point;
    // The ring's last point *is* its first, so moving vertex 0 has to move the
    // repeat with it - letting the two drift apart produces a polygon Django's
    // GEOS parser rejects outright. Only vertex 0: mirroring any other index
    // onto the end would drag an unrelated corner along with it.
    if (isClosedRing(geometry) && index === 0) ring[ring.length - 1] = point;
    return true;
}

/**
 * Remove one vertex, refusing to take a shape below its minimum.
 *
 * @returns true when the vertex was removed.
 */
export function removeVertex(geometry: EditableGeometry, index: number): boolean {
    const ring = ringOf(geometry);
    if (!ring) return false;
    const count = vertexCount(geometry);
    if (index < 0 || index >= count || count <= minimumVertices(geometry)) return false;
    ring.splice(index, 1);
    if (isClosedRing(geometry)) ring[ring.length - 1] = ring[0] as Position;
    return true;
}

/**
 * Insert a vertex after `index` - what dragging a midpoint handle does.
 *
 * @returns true when the vertex was inserted.
 */
export function insertVertex(geometry: EditableGeometry, index: number, point: Position): boolean {
    const ring = ringOf(geometry);
    if (!ring || index < 0 || index >= vertexCount(geometry)) return false;
    ring.splice(index + 1, 0, point);
    return true;
}

/** The midpoint between two positions. */
export function midpoint(from: Position, to: Position): Position {
    return [(from[0] + to[0]) / 2, (from[1] + to[1]) / 2];
}

/**
 * Drop repeated points off the end of a click sequence.
 *
 * A double-click fires two clicks first, so the point that finishes a shape
 * has already been added twice - stored as-is that is a zero-length segment.
 */
export function dropTrailingDuplicates(points: Position[]): Position[] {
    const trimmed = [...points];
    while (trimmed.length >= 2 && samePoint(trimmed[trimmed.length - 1] as Position, trimmed[trimmed.length - 2] as Position)) {
        trimmed.pop();
    }
    return trimmed;
}

/** Close a click sequence into a GeoJSON polygon ring. */
export function closeRing(points: Position[]): Position[] {
    const ring = [...points];
    if (ring.length && !samePoint(ring[0] as Position, ring[ring.length - 1] as Position)) ring.push(ring[0] as Position);
    return ring;
}
