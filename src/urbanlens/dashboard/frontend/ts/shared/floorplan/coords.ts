/**
 * Plan-local metric coordinates, and the conversion to/from WGS-84.
 *
 * Floorplan geometry is authored and stored in metres east/north of a single
 * per-plan origin, not in lat/lng. Degrees are the wrong space to compute in:
 * a degree of longitude is ~74 km at 42 degrees N but 111 km at the equator, so
 * lengths, angles, right-angle snapping and area all come out wrong unless
 * every operation carries a latitude correction. Metres also make the tolerances
 * the editor cares about ("within 0.15 m") expressible as themselves.
 *
 * The origin is shared by every floor of a plan, which is what lets floors be
 * stacked and aligned at all.
 */

/** A point in plan-local space: metres east (x) and north (y) of the origin. */
export interface Pt {
    x: number;
    y: number;
}

/** A WGS-84 coordinate. */
export interface LatLng {
    lat: number;
    lng: number;
}

/** Mean Earth radius (metres), WGS-84 authalic sphere. */
const EARTH_RADIUS_M = 6371008.8;

const toRad = (deg: number): number => (deg * Math.PI) / 180;
const toDeg = (rad: number): number => (rad * 180) / Math.PI;

/**
 * Converter between plan-local metres and WGS-84 for one plan origin.
 *
 * Uses an equirectangular approximation about the origin, with the longitude
 * scale taken at the origin's latitude. Over the extent of a single building
 * (hundreds of metres) its error is far below the precision anyone drawing from
 * memory can supply; it buys exactly the property the editor needs, which is
 * that x and y are the same unit and that unit is a metre.
 */
export class PlanProjection {
    readonly origin: LatLng;
    private readonly metresPerDegLat: number;
    private readonly metresPerDegLng: number;

    constructor(origin: LatLng) {
        this.origin = origin;
        this.metresPerDegLat = (Math.PI / 180) * EARTH_RADIUS_M;
        this.metresPerDegLng = this.metresPerDegLat * Math.cos(toRad(origin.lat));
    }

    /** WGS-84 -> plan-local metres. */
    toLocal(point: LatLng): Pt {
        return {
            x: (point.lng - this.origin.lng) * this.metresPerDegLng,
            y: (point.lat - this.origin.lat) * this.metresPerDegLat,
        };
    }

    /** Plan-local metres -> WGS-84. */
    toWorld(point: Pt): LatLng {
        return {
            lat: this.origin.lat + point.y / this.metresPerDegLat,
            lng: this.origin.lng + point.x / this.metresPerDegLng,
        };
    }
}

// ---------------------------------------------------------------------------
// Vector helpers. All operate in plan-local metres.
// ---------------------------------------------------------------------------

export const sub = (a: Pt, b: Pt): Pt => ({ x: a.x - b.x, y: a.y - b.y });
export const add = (a: Pt, b: Pt): Pt => ({ x: a.x + b.x, y: a.y + b.y });
export const scale = (a: Pt, k: number): Pt => ({ x: a.x * k, y: a.y * k });
export const dot = (a: Pt, b: Pt): number => a.x * b.x + a.y * b.y;
/** 2D cross product (z of the 3D cross) - sign gives turn direction. */
export const cross = (a: Pt, b: Pt): number => a.x * b.y - a.y * b.x;
export const length = (a: Pt): number => Math.hypot(a.x, a.y);
export const distance = (a: Pt, b: Pt): number => Math.hypot(a.x - b.x, a.y - b.y);

/** Angle of the vector a->b, in radians, measured CCW from east. */
export const angleOf = (a: Pt, b: Pt): number => Math.atan2(b.y - a.y, b.x - a.x);

/**
 * Closest point to *p* on segment ab, and how far along ab it lies.
 *
 * Returns the parameter `t` clamped to [0, 1], so `t` doubles as the position
 * of an opening along a wall.
 */
export function projectOnSegment(p: Pt, a: Pt, b: Pt): { point: Pt; t: number; distance: number } {
    const ab = sub(b, a);
    const lengthSquared = dot(ab, ab);
    if (lengthSquared === 0) return { point: a, t: 0, distance: distance(p, a) };
    const t = Math.max(0, Math.min(1, dot(sub(p, a), ab) / lengthSquared));
    const point = add(a, scale(ab, t));
    return { point, t, distance: distance(p, point) };
}

/** Signed area of a ring (positive = counter-clockwise). */
export function signedArea(ring: readonly Pt[]): number {
    let total = 0;
    for (let i = 0; i < ring.length; i++) {
        const a = ring[i] as Pt;
        const b = ring[(i + 1) % ring.length] as Pt;
        total += cross(a, b);
    }
    return total / 2;
}

/** Whether *point* lies inside *ring*, by ray casting. Boundary is undefined. */
export function pointInRing(point: Pt, ring: readonly Pt[]): boolean {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const a = ring[i] as Pt;
        const b = ring[j] as Pt;
        const straddles = a.y > point.y !== b.y > point.y;
        if (!straddles) continue;
        const xAtPointY = ((b.x - a.x) * (point.y - a.y)) / (b.y - a.y) + a.x;
        if (point.x < xAtPointY) inside = !inside;
    }
    return inside;
}

/** Rotate *point* about *about* by *radians* CCW. */
export function rotate(point: Pt, radians: number, about: Pt = { x: 0, y: 0 }): Pt {
    const cos = Math.cos(radians);
    const sin = Math.sin(radians);
    const d = sub(point, about);
    return { x: about.x + d.x * cos - d.y * sin, y: about.y + d.x * sin + d.y * cos };
}
