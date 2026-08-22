/**
 * Derive enclosed rooms from wall centrelines.
 *
 * Walls are the only stored geometry; a room is a *seed point* that binds to
 * whichever enclosed region contains it. Everything here is the derivation from
 * one to the other, and it is recomputed from scratch on every edit rather than
 * maintained incrementally - at the scale of one building (a few hundred
 * segments) a full rebuild is well under a frame, and an incremental planar
 * subdivision is a well-known source of corruption bugs.
 *
 * The pipeline is weld -> planarize -> heal -> extract faces. The split between
 * the second and third steps is the important one:
 *
 *   - Planarizing is exact. Segments that genuinely cross are split at the
 *     crossing; a vertex genuinely on another segment splits it. No input
 *     coordinate moves.
 *   - Healing is forgiving, and deliberately does not move the user's
 *     coordinates either. It records *virtual* joins between endpoints that
 *     nearly meet, so a room closes without the drawing quietly changing
 *     underneath the person who drew it.
 *
 * Nothing here needs a DOM, which is what makes it directly testable.
 */

import { type Pt, angleOf, cross, distance, pointInRing, projectOnSegment, signedArea, sub } from "./coords";

/** One wall centreline as far as geometry is concerned. */
export interface Segment {
    /** Stable id of the wall this segment came from. */
    wallId: string;
    a: Pt;
    b: Pt;
}

/** An enclosed region derived from the wall graph. */
export interface Face {
    /** Boundary ring, counter-clockwise, first point not repeated at the end. */
    ring: Pt[];
    /** Area in square metres. */
    area: number;
    /** Ids of the walls contributing at least one edge to this face. */
    wallIds: string[];
}

/** A join the healer inferred between two endpoints that nearly met. */
export interface HealedJoin {
    at: Pt;
    /** Distance that was bridged, in metres. */
    gap: number;
    /** The two original endpoints the join bridges, for a caller that wants
     * to actually move them together rather than just note where they are -
     * this module itself never does, per the module docstring. */
    a: Pt;
    b: Pt;
}

export interface DeriveOptions {
    /**
     * Endpoints closer than this are treated as the same vertex, in metres.
     * Applies to deliberate snapping done at draw time, so it is small.
     */
    weldTolerance?: number;
    /**
     * Dangling endpoints within this distance of something they could join are
     * healed, in metres. Larger than the weld tolerance: it is forgiving a
     * hand-drawn near-miss rather than merging two points meant to be one.
     */
    healGap?: number;
    /** Faces smaller than this are discarded as slivers, in square metres. */
    minFaceArea?: number;
}

const DEFAULTS: Required<DeriveOptions> = {
    weldTolerance: 0.05,
    healGap: 0.6,
    minFaceArea: 0.25,
};

export interface DeriveResult {
    faces: Face[];
    healed: HealedJoin[];
    /** Endpoints still dangling after healing - the "not enclosed" hints. */
    dangling: Pt[];
}

// ---------------------------------------------------------------------------
// Vertex welding
// ---------------------------------------------------------------------------

/**
 * Quantised spatial hash keyed on a tolerance-sized cell.
 *
 * Checking the 9 cells around a point finds every candidate within one
 * tolerance without the O(n^2) scan a naive weld would do.
 */
class VertexIndex {
    private readonly cells = new Map<string, number[]>();
    private readonly points: Pt[] = [];

    constructor(private readonly cellSize: number) {}

    private key(x: number, y: number): string {
        return `${Math.floor(x / this.cellSize)}:${Math.floor(y / this.cellSize)}`;
    }

    /** Index of an existing point within *tolerance*, or -1. */
    find(p: Pt, tolerance: number): number {
        const cx = Math.floor(p.x / this.cellSize);
        const cy = Math.floor(p.y / this.cellSize);
        let best = -1;
        let bestDistance = tolerance;
        for (let dx = -1; dx <= 1; dx++) {
            for (let dy = -1; dy <= 1; dy++) {
                for (const index of this.cells.get(`${cx + dx}:${cy + dy}`) || []) {
                    const d = distance(p, this.points[index] as Pt);
                    if (d <= bestDistance) {
                        best = index;
                        bestDistance = d;
                    }
                }
            }
        }
        return best;
    }

    /** Index of the point equal to *p* within tolerance, inserting if absent. */
    intern(p: Pt, tolerance: number): number {
        const existing = this.find(p, tolerance);
        if (existing >= 0) return existing;
        const index = this.points.length;
        this.points.push(p);
        const k = this.key(p.x, p.y);
        const bucket = this.cells.get(k);
        if (bucket) bucket.push(index);
        else this.cells.set(k, [index]);
        return index;
    }

    all(): Pt[] {
        return this.points;
    }
}

// ---------------------------------------------------------------------------
// Planarization
// ---------------------------------------------------------------------------

/** Proper intersection point of two segments, or null. Endpoints excluded. */
function segmentIntersection(a1: Pt, a2: Pt, b1: Pt, b2: Pt): Pt | null {
    const r = sub(a2, a1);
    const s = sub(b2, b1);
    const denominator = cross(r, s);
    if (Math.abs(denominator) < 1e-12) return null; // parallel or collinear
    const t = cross(sub(b1, a1), s) / denominator;
    const u = cross(sub(b1, a1), r) / denominator;
    const EPS = 1e-9;
    if (t <= EPS || t >= 1 - EPS || u <= EPS || u >= 1 - EPS) return null;
    return { x: a1.x + r.x * t, y: a1.y + r.y * t };
}

/**
 * Split every segment at real crossings and at vertices lying on its interior.
 *
 * The second half is what makes interior partitions work: people draw a
 * partition wall that ends *on* an exterior wall rather than at one of its
 * corners, and without splitting the exterior wall there the two never share a
 * vertex and no room ever closes.
 */
function planarize(segments: Segment[], tolerance: number): Segment[] {
    // Cut points per segment, as parameters along it.
    const cuts: number[][] = segments.map(() => []);

    for (let i = 0; i < segments.length; i++) {
        for (let j = i + 1; j < segments.length; j++) {
            const si = segments[i] as Segment;
            const sj = segments[j] as Segment;
            const hit = segmentIntersection(si.a, si.b, sj.a, sj.b);
            if (!hit) continue;
            cuts[i]?.push(projectOnSegment(hit, si.a, si.b).t);
            cuts[j]?.push(projectOnSegment(hit, sj.a, sj.b).t);
        }
    }

    // T-junctions: an endpoint sitting on another segment's interior.
    const endpoints: Pt[] = [];
    for (const s of segments) endpoints.push(s.a, s.b);
    for (let i = 0; i < segments.length; i++) {
        const s = segments[i] as Segment;
        for (const p of endpoints) {
            const near = projectOnSegment(p, s.a, s.b);
            if (near.distance > tolerance) continue;
            if (near.t <= 1e-9 || near.t >= 1 - 1e-9) continue;
            cuts[i]?.push(near.t);
        }
    }

    const out: Segment[] = [];
    for (let i = 0; i < segments.length; i++) {
        const s = segments[i] as Segment;
        const ts = [0, ...(cuts[i] || []), 1].sort((x, y) => x - y);
        for (let k = 0; k < ts.length - 1; k++) {
            const t0 = ts[k] as number;
            const t1 = ts[k + 1] as number;
            if (t1 - t0 < 1e-9) continue;
            const at = (t: number): Pt => ({ x: s.a.x + (s.b.x - s.a.x) * t, y: s.a.y + (s.b.y - s.a.y) * t });
            out.push({ wallId: s.wallId, a: at(t0), b: at(t1) });
        }
    }
    return out;
}

// ---------------------------------------------------------------------------
// Graph
// ---------------------------------------------------------------------------

interface Graph {
    points: Pt[];
    /** Undirected edges as vertex index pairs, deduplicated. */
    edges: Array<{ u: number; v: number; wallId: string }>;
}

function buildGraph(segments: Segment[], weldTolerance: number): Graph {
    const index = new VertexIndex(Math.max(weldTolerance, 1e-6) * 4);
    const seen = new Set<string>();
    const edges: Graph["edges"] = [];
    for (const s of segments) {
        const u = index.intern(s.a, weldTolerance);
        const v = index.intern(s.b, weldTolerance);
        if (u === v) continue; // zero-length after welding
        const key = u < v ? `${u}-${v}` : `${v}-${u}`;
        if (seen.has(key)) continue;
        seen.add(key);
        edges.push({ u, v, wallId: s.wallId });
    }
    return { points: index.all(), edges };
}

/**
 * Bridge endpoints that nearly meet, so a hand-drawn near-miss still encloses.
 *
 * Only degree-1 vertices are considered: a corner someone already closed is
 * not a candidate, and this keeps the healer from inventing joins in the middle
 * of a tidy drawing. The join is added as an extra edge rather than by moving
 * either endpoint, so what the user drew is preserved exactly.
 */
function heal(graph: Graph, gap: number): HealedJoin[] {
    const degree = new Map<number, number>();
    for (const e of graph.edges) {
        degree.set(e.u, (degree.get(e.u) || 0) + 1);
        degree.set(e.v, (degree.get(e.v) || 0) + 1);
    }
    const dangling = graph.points.map((_, i) => i).filter((i) => (degree.get(i) || 0) === 1);
    const healed: HealedJoin[] = [];
    const joined = new Set<number>();

    for (let i = 0; i < dangling.length; i++) {
        const a = dangling[i] as number;
        if (joined.has(a)) continue;
        let best = -1;
        let bestDistance = gap;
        for (let j = i + 1; j < dangling.length; j++) {
            const b = dangling[j] as number;
            if (joined.has(b)) continue;
            const d = distance(graph.points[a] as Pt, graph.points[b] as Pt);
            if (d < bestDistance) {
                best = b;
                bestDistance = d;
            }
        }
        if (best < 0) continue;
        joined.add(a);
        joined.add(best);
        graph.edges.push({ u: a, v: best, wallId: "__healed__" });
        const pa = graph.points[a] as Pt;
        const pb = graph.points[best] as Pt;
        healed.push({ at: { x: (pa.x + pb.x) / 2, y: (pa.y + pb.y) / 2 }, gap: bestDistance, a: pa, b: pb });
    }
    return healed;
}

// ---------------------------------------------------------------------------
// Face extraction
// ---------------------------------------------------------------------------

/**
 * Walk the half-edge structure and return every bounded face.
 *
 * At each vertex the outgoing edges are sorted by angle; from an incoming
 * half-edge the traversal leaves along the neighbour immediately clockwise from
 * where it arrived. Following that consistently traces each bounded face once,
 * counter-clockwise, and the unbounded outer face once clockwise - so the outer
 * face falls out simply by discarding negative signed area.
 */
function extractFaces(graph: Graph, minArea: number): Face[] {
    const neighbours = new Map<number, number[]>();
    const wallOf = new Map<string, string>();
    for (const e of graph.edges) {
        if (!neighbours.has(e.u)) neighbours.set(e.u, []);
        if (!neighbours.has(e.v)) neighbours.set(e.v, []);
        neighbours.get(e.u)?.push(e.v);
        neighbours.get(e.v)?.push(e.u);
        wallOf.set(`${e.u}->${e.v}`, e.wallId);
        wallOf.set(`${e.v}->${e.u}`, e.wallId);
    }
    // Sort each vertex's neighbours by angle, CCW from east.
    for (const [v, list] of neighbours) {
        const origin = graph.points[v] as Pt;
        list.sort((p, q) => angleOf(origin, graph.points[p] as Pt) - angleOf(origin, graph.points[q] as Pt));
    }

    const visited = new Set<string>();
    const faces: Face[] = [];

    for (const start of graph.edges) {
        for (const [from, to] of [
            [start.u, start.v],
            [start.v, start.u],
        ] as Array<[number, number]>) {
            if (visited.has(`${from}->${to}`)) continue;
            const ring: number[] = [];
            const walls = new Set<string>();
            let u = from;
            let v = to;
            let guard = 0;
            while (guard++ < graph.edges.length * 4) {
                if (visited.has(`${u}->${v}`)) break;
                visited.add(`${u}->${v}`);
                ring.push(u);
                const wall = wallOf.get(`${u}->${v}`);
                if (wall && wall !== "__healed__") walls.add(wall);
                const around = neighbours.get(v) as number[];
                const back = around.indexOf(u);
                // One step clockwise from the edge we arrived on.
                const next = around[(back - 1 + around.length) % around.length] as number;
                u = v;
                v = next;
                if (u === from && v === to) break;
            }
            if (ring.length < 3) continue;
            const points = ring.map((i) => graph.points[i] as Pt);
            const area = signedArea(points);
            if (area <= minArea) continue; // negative = outer face; tiny = sliver
            faces.push({ ring: points, area, wallIds: [...walls] });
        }
    }
    return faces;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Derive the enclosed regions formed by a set of wall centrelines.
 *
 * Args:
 *     segments: Wall centrelines in plan-local metres.
 *     options: Tolerances; see :data:`DEFAULTS`.
 *
 * Returns:
 *     The bounded faces, the joins healing inferred, and any endpoint still
 *     left dangling (which the editor surfaces as "not enclosed").
 */
export function deriveFaces(segments: Segment[], options: DeriveOptions = {}): DeriveResult {
    const { weldTolerance, healGap, minFaceArea } = { ...DEFAULTS, ...options };
    if (!segments.length) return { faces: [], healed: [], dangling: [] };

    const split = planarize(segments, weldTolerance);
    const graph = buildGraph(split, weldTolerance);
    const healed = heal(graph, healGap);
    const faces = extractFaces(graph, minFaceArea);

    const degree = new Map<number, number>();
    for (const e of graph.edges) {
        degree.set(e.u, (degree.get(e.u) || 0) + 1);
        degree.set(e.v, (degree.get(e.v) || 0) + 1);
    }
    const dangling = graph.points.filter((_, i) => (degree.get(i) || 0) === 1);

    return { faces, healed, dangling };
}

/**
 * The smallest face containing *seed*, or null when it encloses nothing.
 *
 * Smallest rather than first because faces nest: a seed inside a cupboard that
 * sits inside a hall is in both, and the cupboard is the room the user meant.
 */
export function faceForSeed(seed: Pt, faces: readonly Face[]): Face | null {
    let best: Face | null = null;
    for (const face of faces) {
        if (!pointInRing(seed, face.ring)) continue;
        if (!best || face.area < best.area) best = face;
    }
    return best;
}
