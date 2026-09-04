/**
 * Where the parcel on the map came from - not merely whether one is drawn.
 *
 * `hrsh-boundary.spec.ts` asks whether a boundary arrives. It can pass while
 * the product is badly wrong, and on this deployment it did: a boundary was
 * drawn, it was roughly the right size, it contained the pin - and it was a
 * shape the application had invented rather than one any provider offered.
 *
 * ## What was actually wrong
 *
 * REData answers six scored candidates for this parcel and flags one
 * `is_suggested`. The app drew none of them. It drew the convex hull of the
 * campus pin and its three child pins - an outline of *the markers we happen to
 * know about*, which is a drawing of our own ignorance rather than evidence
 * about the world. It is a legitimate last resort and a terrible thing to
 * prefer, because it is indistinguishable on screen from a real parcel and
 * silently wrong by however much the building set is incomplete.
 *
 * Three defects compounded, and each needed its own guard because fixing any
 * one alone still left the wrong shape on the map:
 *
 * 1. **The chain was never asked.** `resolve_location_place` consults places
 *    already on record and calls no provider - its own docstring says so - yet
 *    it stamped `Location.place_resolved_at`, which `generation_status` reads
 *    as "the provider chain ran". Every pin was therefore born marked as
 *    already-enriched: generation was never scheduled, the boundary panel
 *    reported itself ready, and REData went uncalled until the stamp went
 *    stale 60 days later.
 * 2. **Our hull outranked their parcel.** `resolve_for_pin` returned the pin's
 *    own `generated_polygon` before consulting the place, so even geometry that
 *    did arrive stayed invisible on the page that fetched it.
 * 3. **Nothing superseded the stand-in.** The hull row survived the arrival of
 *    real geometry and was refitted on every hierarchy change.
 *
 * ## Why the pytest suite could not catch this
 *
 * All three are agreements between our cache-keeping and a live provider's
 * answers. A unit test supplies the provider's answer itself, so it can only
 * ever confirm the arrangement it already assumes; nothing in-process can
 * notice "REData had six candidates and we drew none of them". That is the
 * structural gap `docs/audits/TEST_COVERAGE_GAPS.md` exists to record, and it is why
 * these assertions live here rather than there.
 *
 * The counterpart unit tests - which pin the precedence rules themselves, and
 * run in CI - are in
 * `dashboard/tests/hypothesis/test_redata_parcel_beats_generated_hull.py`.
 */

import { allPins, expect, locationDataTest as test, skipUnlessLocationDataEnabled } from "./fixtures.js";
import { approximateAreaSqm, type GeoJsonGeometry, hrshRoutes } from "../../lib/hrsh.js";

skipUnlessLocationDataEnabled();

interface BoundaryPayload {
    pending?: boolean;
    refreshing?: boolean;
    boundaries?: {
        property?: { polygon?: GeoJsonGeometry | null; source?: string | null };
        building?: { polygon?: GeoJsonGeometry | null; source?: string | null };
    };
}

/**
 * Boundary sources that mean "a provider told us this".
 *
 * `place` is official geometry the chain fetched and voting materialised onto
 * the Place. `pin` and `wiki` are geometry a person drew deliberately, which
 * outranks providers by design and is equally not an invention of ours.
 */
const HONEST_SOURCES = new Set(["place", "pin", "wiki", "inherited"]);

/**
 * The source that means "we made this shape up".
 *
 * Correct only while no provider has offered an outline - see the module
 * docstring. Its appearance next to a resolvable parcel is the reported bug.
 */
const INVENTED_SOURCE = "generated";

async function boundaryPayload(page: { request: { get: (url: string) => Promise<{ status: () => number; json: () => Promise<unknown> }> } }, pinSlug: string): Promise<BoundaryPayload> {
    const response = await page.request.get(hrshRoutes.pinBoundary(pinSlug));
    expect(response.status(), "the pin page's boundary endpoint did not answer").toBe(200);
    return (await response.json()) as BoundaryPayload;
}

test.describe("Hudson River State Hospital - where the parcel came from", () => {
    test("the outline on the map is one a provider supplied", async ({ campus, page }) => {
        campus.requireBoundary();
        const payload = await boundaryPayload(page, campus.pin.slug);
        const source = payload.boundaries?.property?.source ?? null;

        expect(
            source,
            `the map is drawing a boundary whose source is ${JSON.stringify(source)}. REData offers several scored candidates for this ` +
                'parcel and flags one of them is_suggested, so "generated" here means the application preferred a shape it invented over ' +
                "one it was given. That is the reported defect, and it is invisible to the eye: the invented hull is plausibly sized and " +
                "contains the pin. Check in order - did the chain run at all (Location.place_resolved_at vs place_id), did it store a " +
                "Place with geometry, and does resolve_for_pin still return the pin's own generated_polygon ahead of the place",
        ).not.toBe(INVENTED_SOURCE);

        expect(
            HONEST_SOURCES.has(source ?? ""),
            `the property boundary's source is ${JSON.stringify(source)}, which is neither a provider's outline nor a person's drawing. ` +
                '"circle" means the 50 m default is still standing in and no parcel was ever resolved',
        ).toBe(true);
    });

    test("the outline is not a hull fitted around this pin's own child pins", async ({ campus, page }) => {
        // The sharp version of the test above, and the one that would still
        // fail if `source` were reported correctly while the geometry stayed
        // wrong. Deliberately derives the hull from live data rather than
        // hard-coding an area: REData's suggested candidate for this parcel is
        // expected to change (its own boundary selection is being corrected),
        // and a test pinned to today's number would fail for that instead.
        campus.requireBoundary();

        const pins = await allPins(campus.api);
        const children = pins.filter((row) => row.parent_uuid === campus.pin.uuid);
        test.skip(children.length < 2, "fewer than two child pins exist, so no hull could have been fitted around them.");

        const markers: Array<[number, number]> = [[campus.origin.longitude, campus.origin.latitude], ...children.map((row): [number, number] => [row.longitude, row.latitude])];
        // Padded before hulling, because the application pads: `_fitted_polygon`
        // buffers every marker by CHILD_BOUNDARY_PADDING_METERS and hulls the
        // resulting rings, so a hull of the bare points is materially smaller
        // and the comparison below silently passes. It did exactly that on the
        // first run against known-broken data - the reason this reproduces the
        // padding rather than approximating it.
        const padded = markers.flatMap((marker) => ringAround(marker, CHILD_BOUNDARY_PADDING_METERS));
        const hullArea = approximateAreaSqm({ type: "Polygon", coordinates: [convexHull(padded)] });
        const drawnArea = approximateAreaSqm(payloadPolygon(await boundaryPayload(page, campus.pin.slug)));

        // A convex hull of n points is not going to coincide with a surveyed
        // parcel to within a few percent by accident, so near-equality is
        // strong evidence the hull is what is being drawn.
        const ratio = drawnArea > 0 && hullArea > 0 ? drawnArea / hullArea : 0;
        expect(
            Math.abs(ratio - 1) > 0.05,
            `the boundary being drawn covers ${Math.round(drawnArea).toLocaleString()} m² and the convex hull of this pin's own ` +
                `${markers.length} markers covers ${Math.round(hullArea).toLocaleString()} m² - within 5% of each other. The map is ` +
                "almost certainly drawing the hull, which grows and shrinks with the set of buildings we happen to have imported rather " +
                "than describing the property at all",
        ).toBe(true);
    });

    test("a pin on ground nobody has asked about is not born already-enriched", async ({ campus, page }) => {
        // The root cause, isolated - and the only test here that needs no
        // enrichment to have finished, so it stays meaningful even when the
        // campus fixture found no parcel.
        //
        // A randomised coordinate rather than a constant: `Location` rows are
        // keyed by coordinate and outlive the pins that created them, so a
        // fixed one would carry the previous run's `place_resolved_at` and the
        // test would assert nothing on its second execution.
        const latitude = 41.62 + Math.random() * 0.02;
        const longitude = -73.82 - Math.random() * 0.02;

        const created = await campus.api.post("pins/", {
            name: "e2e boundary-provenance virgin-ground probe",
            latitude,
            longitude,
            name_is_user_provided: true,
        });
        expect(created.ok(), `could not create the probe pin: HTTP ${created.status()} ${(await created.text()).slice(0, 200)}`).toBe(true);
        const probe = (await created.json()) as { slug: string };

        try {
            const payload = await boundaryPayload(page, probe.slug);
            const source = payload.boundaries?.property?.source ?? null;
            const settled = payload.pending !== true && payload.refreshing !== true;

            expect(
                settled && source === "circle",
                `a pin created seconds ago at ${latitude.toFixed(5)}, ${longitude.toFixed(5)} already reports pending=${payload.pending}, ` +
                    `refreshing=${payload.refreshing}, source=${JSON.stringify(source)}. That combination is the application saying "the ` +
                    'providers have been asked and there is nothing here" about a coordinate nothing has ever looked at. It is how the ' +
                    "campus pin ended up with an invented boundary: resolve_location_place stamps place_resolved_at without calling a " +
                    "provider, generation_status reads that stamp as proof the chain ran, and so it never runs",
            ).toBe(false);
        } finally {
            // Left behind, this pin would occupy a coordinate and profile slot
            // on every future run of the suite.
            await campus.api.delete(`pins/${probe.slug}/`);
        }
    });
});

/**
 * Metres of breathing room the application adds around each child marker.
 *
 * Mirrors `CHILD_BOUNDARY_PADDING_METERS` in
 * `services/geo/child_pin_boundaries.py`. If that constant changes and this one
 * does not, the comparison above loses its sharpness rather than failing - so
 * the test reports the two areas it measured, which makes the drift legible.
 */
const CHILD_BOUNDARY_PADDING_METERS = 10;

/** Points on a circle of *radius* metres around a lon/lat pair. */
function ringAround([lon, lat]: [number, number], radius: number, segments = 16): Array<[number, number]> {
    const metresPerDegreeLat = 111_320;
    const metresPerDegreeLon = metresPerDegreeLat * Math.cos((lat * Math.PI) / 180);
    return Array.from({ length: segments }, (_unused, index) => {
        const angle = (2 * Math.PI * index) / segments;
        return [lon + (radius * Math.cos(angle)) / metresPerDegreeLon, lat + (radius * Math.sin(angle)) / metresPerDegreeLat] as [number, number];
    });
}

/** The property polygon from a boundary payload, or null. */
function payloadPolygon(payload: BoundaryPayload): GeoJsonGeometry | null {
    return payload.boundaries?.property?.polygon ?? null;
}

/**
 * Convex hull of a set of lon/lat pairs, as a closed ring (monotone chain).
 *
 * Planar rather than spherical, which is exact enough over a few hundred
 * metres: this only has to reproduce the same hull the application fits, and
 * that one is planar too.
 */
function convexHull(points: Array<[number, number]>): Array<[number, number]> {
    const sorted = [...points].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
    if (sorted.length < 3) {
        return [...sorted, sorted[0]!];
    }
    const cross = (o: [number, number], a: [number, number], b: [number, number]): number => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);

    const build = (source: Array<[number, number]>): Array<[number, number]> => {
        const line: Array<[number, number]> = [];
        for (const point of source) {
            while (line.length >= 2 && cross(line[line.length - 2]!, line[line.length - 1]!, point) <= 0) {
                line.pop();
            }
            line.push(point);
        }
        line.pop();
        return line;
    };

    const hull = [...build(sorted), ...build([...sorted].reverse())];
    return [...hull, hull[0]!];
}
