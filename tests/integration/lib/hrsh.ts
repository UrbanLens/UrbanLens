/**
 * Hudson River State Hospital, as a test subject.
 *
 * This one place has been the development reference for the whole application,
 * so the specs under `specs/location/` ask of it the questions that are hard to
 * ask of a synthetic fixture: does a coordinate resolve to the right *property*,
 * does the parcel line look like a parcel, do buildings become child pins, does
 * the wiki fill itself in. None of that can be answered by a pin dropped in a
 * field - the answers come from county assessor data, NY SHPO's CRIS inventory,
 * EPA ECHO and Wikipedia, and those only exist for somewhere real.
 *
 * ## What is asserted, and what is deliberately not
 *
 * These tests run against live third-party data, which moves. A test that
 * hardcodes what the data said on the day it was written starts failing for the
 * wrong reason, and the person who sees it fail has no way to tell "the app
 * broke" from "the county recorded a sale". So the rule here is:
 *
 * - **Assert the invariant, not the value.** "Every one of these coordinates
 *   resolves to the same property" is a property of the app. "The parcel is
 *   4.2 hectares" is a property of the county's GIS file this week.
 * - **Bound, don't equal.** Where a number has to be checked, check that it is
 *   in a range wide enough to survive a data revision and narrow enough to catch
 *   the failure that actually happens - see {@link EXPECTED_PARCEL_AREA_SQM}.
 * - **Order, don't pin.** For the sale history, assert that the most recent sale
 *   is the newest one on file and is not in the future, never that it is a
 *   particular date. A new sale must not turn this suite red.
 *
 * ## The values below are expectations, not measurements
 *
 * Everything here that is not a coordinate came from either the requirements
 * this suite was written to or from public reporting, **not** from reading it
 * out of a running instance. That is deliberate: reading the expected value out
 * of the system under test is how a suite ends up certifying whatever it
 * happens to do. Each one carries where it came from, so a failure can be
 * argued with rather than just re-pointed at the app.
 */

/** A coordinate, as the API takes it. */
export interface Coordinate {
    readonly label: string;
    readonly latitude: number;
    readonly longitude: number;
}

/**
 * Five points inside the hospital's official parcel.
 *
 * Supplied as the requirement: a user dropping a pin on any of these has
 * pinned the same place, because the property boundary contains all five. They
 * are spread over roughly 340 m north-south and 410 m east-west - far enough
 * apart that nothing distance-based could accidentally unify them, which is the
 * point. The app must reach this answer from the parcel polygon, not from
 * proximity.
 */
export const INSIDE_BOUNDARY: readonly Coordinate[] = [
    { label: "west range", latitude: 41.733181, longitude: -73.928493 },
    { label: "central", latitude: 41.733245, longitude: -73.927088 },
    { label: "north east", latitude: 41.734481, longitude: -73.925463 },
    { label: "south west", latitude: 41.731435, longitude: -73.926761 },
    { label: "east range", latitude: 41.733453, longitude: -73.923558 },
] as const;

/**
 * Four points outside it, which must resolve somewhere else.
 *
 * The negative half, and the half that makes the positive half mean anything:
 * a boundary that contained the whole neighbourhood would satisfy every
 * "same place" assertion and be completely wrong.
 *
 * The first is the one that matters. At ~565 m from the centre of the five it
 * is only just outside a parcel of this size, so it fails against a boundary
 * that is merely approximately right. The other three are 1.6-1.9 km away and
 * would only be caught by something badly wrong - the ~1,040-acre CRIS
 * archaeological sensitivity zone that
 * `services.apis.locations.boundaries.redata` documents as a real candidate
 * for this kind of site, for instance.
 */
export const OUTSIDE_BOUNDARY: readonly Coordinate[] = [
    { label: "just north of the campus", latitude: 41.738224, longitude: -73.925801 },
    { label: "south west", latitude: 41.719043, longitude: -73.932924 },
    { label: "south east", latitude: 41.721957, longitude: -73.908925 },
    { label: "north east", latitude: 41.73818, longitude: -73.908039 },
] as const;

/**
 * A point on one of the campus buildings, used for the floorplan assertions.
 *
 * Within ~5 m of {@link INSIDE_BOUNDARY}'s first entry, so it is the same
 * structure that coordinate stands on.
 */
export const BUILDING_COORDINATE: Coordinate = { label: "building with a known footprint", latitude: 41.733147, longitude: -73.928536 };

/** Mean of {@link INSIDE_BOUNDARY}, for distance assertions. */
export const CAMPUS_CENTRE: Coordinate = { label: "campus centre", latitude: 41.733159, longitude: -73.926273 };

/**
 * Plausible bounds for the parcel's area, in square metres.
 *
 * **Grounded on a measurement, not on press coverage.** REData's authoritative
 * boundary for 41.733181, -73.928493 is **133,964 m² (33.1 acres)**, read
 * straight from `RedataBoundaryProvider.get_typed_boundaries` against the live
 * service. That is the parcel the coordinate stands on.
 *
 * An earlier version of this file used the widely reported "156-acre property"
 * figure and set the floor at 200,000 m². That was wrong in a way worth
 * recording: 156 acres describes the whole Hudson Heritage redevelopment site,
 * which the county splits into several parcels, and the floor it produced
 * **rejected the correct parcel** - which made the fixture report "no geometry
 * arrived" and skip most of this directory. Press figures describe projects;
 * assessors describe parcels, and they are not the same unit.
 *
 * The range around the measurement is wide because it is not checking accuracy -
 * it is catching the two failures `boundaries/redata.py` documents for this kind
 * of site:
 *
 * - **Far too small.** A single building footprint (~1,000-10,000 m²) or a CRIS
 *   consultation polygon chosen instead of the parcel.
 * - **Absurdly too large.** The ~1,040-acre (4.2 km²) archaeological sensitivity
 *   zone, which that module names as a candidate genuinely returned for a parcel
 *   like this one.
 *
 * A revision to the county line, or a neighbouring parcel being merged in, will
 * not move it outside these.
 */
export const EXPECTED_PARCEL_AREA_SQM = { min: 50_000, max: 1_500_000 } as const;

/** What REData actually returns for this coordinate, for failure messages. */
export const MEASURED_PARCEL_AREA_SQM = 133_964;

/**
 * The acreage public reporting gives the redevelopment, for context only.
 *
 * Never asserted against, and deliberately not used to derive
 * {@link EXPECTED_PARCEL_AREA_SQM} - see that constant for why doing so was a
 * mistake. Kept so a failure message can distinguish "the project" from "the
 * parcel" when someone inevitably compares the two.
 */
export const REPORTED_PROJECT_ACREAGE = 156;

/**
 * Owner name expected on the current record.
 *
 * **Treat as unconfirmed.** It is the name given in this suite's requirements,
 * and public reporting is not unambiguous about it: "Hudson Heritage" is
 * certainly the redevelopment's name and was the 2005 purchaser, while more
 * recent coverage names EFG-Saber Heritage SC, LLC as the entity running the
 * project. Those are not necessarily in conflict - a deed holder and a
 * developer are different things - but it does mean a mismatch here is a
 * question for a human, not automatically an application defect.
 *
 * The specs therefore report a mismatch with both names in the message rather
 * than asserting equality, and assert only that *an* official owner record
 * exists. See `specs/location/hrsh-property-data.spec.ts`.
 */
export const EXPECTED_OWNER_FRAGMENT = "Hudson Heritage";

/** Names seen in public reporting, listed in failure messages to aid triage. */
export const KNOWN_OWNER_CANDIDATES = ["Hudson Heritage", "EFG-Saber Heritage", "Diversified Realty", "Saber Real Estate"] as const;

/**
 * Earliest sale date that could be genuine, as an ISO date.
 *
 * The state sold the property in 2005; nothing before that is a private sale
 * of this parcel. Used as a sanity floor so a parser returning epoch-zero, or a
 * timezone bug shifting a date by centuries, is caught. Not a statement about
 * what the most recent sale is.
 */
export const EARLIEST_PLAUSIBLE_SALE = "2000-01-01";

/** Site-relative paths for the surfaces these specs exercise. */
export const hrshRoutes = {
    /** The pin detail page's boundary payload - the richest view of place resolution. */
    pinBoundary: (pinSlug: string) => `/dashboard/map/pin/${pinSlug}/boundary/`,
    /** The floorplan editor for one pin's building. */
    floorplan: (pinSlug: string) => `/dashboard/map/pin/${pinSlug}/floorplan/`,
    /** The community wiki page for a Location. */
    wiki: (locationSlug: string) => `/dashboard/location/${locationSlug}/wiki/`,
    /** The wiki's ownership card (session-authenticated, subscription-gated). */
    wikiOwnership: (locationSlug: string) => `/dashboard/location/${locationSlug}/wiki/ownership/`,
    /** The wiki's sale-history card. */
    wikiSales: (locationSlug: string) => `/dashboard/location/${locationSlug}/wiki/sales/`,
    /** The pin's sale-history tab. */
    pinSales: (pinSlug: string) => `/dashboard/map/pin/${pinSlug}/sales/`,
} as const;

/** Metres between two coordinates, on a sphere. Good to well under a metre here. */
export function metresBetween(a: Coordinate, b: Coordinate): number {
    const earthRadius = 6_371_000;
    const toRadians = (degrees: number) => (degrees * Math.PI) / 180;
    const lat1 = toRadians(a.latitude);
    const lat2 = toRadians(b.latitude);
    const deltaLat = lat2 - lat1;
    const deltaLon = toRadians(b.longitude - a.longitude);
    const h = Math.sin(deltaLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(deltaLon / 2) ** 2;
    return 2 * earthRadius * Math.asin(Math.sqrt(h));
}

/** A GeoJSON geometry, as the API returns one. */
export interface GeoJsonGeometry {
    type: string;
    coordinates: unknown;
}

/** Every `[longitude, latitude]` pair in a GeoJSON geometry, at any nesting depth. */
export function geometryPositions(geometry: GeoJsonGeometry | null | undefined): Array<[number, number]> {
    const positions: Array<[number, number]> = [];
    const walk = (node: unknown): void => {
        if (!Array.isArray(node)) {
            return;
        }
        if (node.length === 2 && typeof node[0] === "number" && typeof node[1] === "number") {
            positions.push([node[0], node[1]]);
            return;
        }
        for (const child of node) {
            walk(child);
        }
    };
    walk(geometry?.coordinates);
    return positions;
}

/**
 * Whether a coordinate falls inside a GeoJSON polygon or multipolygon.
 *
 * A ray-casting test over each ring. Written here rather than pulled in as a
 * dependency because the suite has none for geometry and this is twenty lines -
 * but note what it does *not* do: it ignores interior rings (holes), and it
 * treats longitude/latitude as planar. Both are fine at the scale of one parcel
 * and would not be for a country.
 *
 * @param geometry A GeoJSON Polygon or MultiPolygon.
 * @param point The coordinate to test.
 * @returns True when the point is inside any of the geometry's outer rings.
 */
export function containsCoordinate(geometry: GeoJsonGeometry | null | undefined, point: Coordinate): boolean {
    if (!geometry) {
        return false;
    }
    const rings: Array<Array<[number, number]>> = [];
    const collectRings = (node: unknown, depth: number): void => {
        if (!Array.isArray(node)) {
            return;
        }
        // A ring is an array of positions; depth tells us when we have reached one.
        if (node.length > 0 && Array.isArray(node[0]) && node[0].length === 2 && typeof node[0][0] === "number") {
            rings.push(node as Array<[number, number]>);
            return;
        }
        for (const child of node) {
            collectRings(child, depth + 1);
        }
    };
    collectRings(geometry.coordinates, 0);

    return rings.some((ring) => {
        let inside = false;
        for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
            const [xi, yi] = ring[i]!;
            const [xj, yj] = ring[j]!;
            const straddles = yi > point.latitude !== yj > point.latitude;
            if (straddles && point.longitude < ((xj - xi) * (point.latitude - yi)) / (yj - yi) + xi) {
                inside = !inside;
            }
        }
        return inside;
    });
}

/**
 * Approximate area of a GeoJSON polygon in square metres.
 *
 * The shoelace formula on longitude/latitude scaled to metres at this latitude.
 * Accurate to a percent or so over a parcel, which is far better than the
 * assertions it feeds need - {@link EXPECTED_PARCEL_AREA_SQM} spans a factor of
 * ten. Interior rings are not subtracted, so a doughnut reads slightly large.
 *
 * @param geometry A GeoJSON Polygon or MultiPolygon.
 * @returns Total area of the outer rings, in square metres.
 */
export function approximateAreaSqm(geometry: GeoJsonGeometry | null | undefined): number {
    const positions = geometryPositions(geometry);
    if (positions.length < 3) {
        return 0;
    }
    const meanLatitude = positions.reduce((total, [, lat]) => total + lat, 0) / positions.length;
    const metresPerDegreeLat = 111_132.92 - 559.82 * Math.cos((2 * meanLatitude * Math.PI) / 180);
    const metresPerDegreeLon = 111_412.84 * Math.cos((meanLatitude * Math.PI) / 180);

    const rings: Array<Array<[number, number]>> = [];
    const collectRings = (node: unknown): void => {
        if (!Array.isArray(node)) {
            return;
        }
        if (node.length > 0 && Array.isArray(node[0]) && node[0].length === 2 && typeof node[0][0] === "number") {
            rings.push(node as Array<[number, number]>);
            return;
        }
        for (const child of node) {
            collectRings(child);
        }
    };
    collectRings(geometry?.coordinates);

    return rings.reduce((total, ring) => {
        let doubleArea = 0;
        for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
            const [xi, yi] = ring[i]!;
            const [xj, yj] = ring[j]!;
            doubleArea += (xj * metresPerDegreeLon) * (yi * metresPerDegreeLat) - (xi * metresPerDegreeLon) * (yj * metresPerDegreeLat);
        }
        return total + Math.abs(doubleArea) / 2;
    }, 0);
}
