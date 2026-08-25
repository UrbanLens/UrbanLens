/**
 * Does the parcel boundary ever arrive, and does it reach the map?
 *
 * This file is the one that reports the absence. Everything else under
 * `specs/location/` skips when there is no parcel geometry and points here, so
 * a pipeline that has stopped running produces a single failure naming the
 * cause rather than thirty saying "undefined".
 *
 * ## Why this is the likeliest thing in the directory to fail
 *
 * Reading the code, there is a specific reason to expect it. Boundary
 * provisioning is lazy by design - the comment on `BoundaryPanelSource` says so:
 * "the lazy path that replaced eager generation on pin creation - the provider
 * chain only runs when someone actually views a pin detail page (or creates a
 * wiki)". Every trigger for that chain asks the same question:
 *
 * ```python
 * # services/locations/boundaries.py:208, in generation_status
 * if location.place_resolved_at is None:
 *     return False, False        # -> (ran=False), so scheduling proceeds
 * ```
 *
 * But `create_pin_for_profile` calls `resolve_location_place`, which stamps
 * that timestamp *without calling any provider* - its own docstring says
 * "Never calls a provider - it only asks what is already known", and it stamps
 * even when it found nothing:
 *
 * ```python
 * # services/places/resolution.py:44-46
 * place = Place.objects.resolve_for_point(...)      # may be None
 * if save and (... or location.place_resolved_at is None):
 *     Location.objects.filter(pk=location.pk).update(place=place, place_resolved_at=timezone.now())
 * ```
 *
 * So by the time anyone can view the pin, `boundary_generation_ran` is already
 * True, `schedule_location_boundary_generation` returns "fresh, nothing to do",
 * and `BoundaryPanelSource.is_ready` is True. The chain has never run and
 * nothing will run it until the stamp goes stale, which is
 * `SiteSettings.boundary_cache_days` away.
 *
 * That is a reading of the source, not a measurement - which is exactly why it
 * is written as a test. If it passes, the reading is wrong and this comment
 * should be deleted. If it fails, the failure is the evidence.
 */

import { expect, locationDataTest as test, skipUnlessLocationDataEnabled } from "./fixtures.js";
import { approximateAreaSqm, CAMPUS_CENTRE, containsCoordinate, COUNTY_PARCEL_COVERS, geometryPositions, hrshRoutes, INSIDE_BOUNDARY, metresBetween } from "../../lib/hrsh.js";

skipUnlessLocationDataEnabled();

/**
 * Third-party map-tile origins whose failures are not this spec's subject.
 *
 * The pin detail page's satellite and street layers come from origins that fail
 * in this environment - ArcGIS tiles are blocked by Chrome's Opaque Response
 * Blocking, an OpenStreetMap tile aborts - and the console guard fails any test
 * whose page logged a failed subresource. Narrowed rather than tolerated
 * wholesale; `specs/services/third-party-origins.spec.ts` asks that question on
 * purpose.
 */
const THIRD_PARTY_TILE_HOSTS = [/wayback\.maptiles\.arcgis\.com/, /tile\.openstreetmap\.org/, /server\.arcgisonline\.com/];

test.describe("Hudson River State Hospital - the parcel boundary", () => {
    test.beforeEach(async ({ guard }) => {
        for (const host of THIRD_PARTY_TILE_HOSTS) {
            guard.allow(host);
        }
    });

    test("a pin on the campus is given the campus parcel", async ({ campus }) => {
        // The one test in this directory that reports missing geometry as a
        // failure rather than skipping. `campus.diagnosis` carries what was
        // observed while waiting, and the fixture attaches a timestamped log.
        expect(campus.boundary, campus.diagnosis || "no parcel geometry, and no diagnosis was recorded").not.toBeNull();
    });

    test("the boundary is a closed polygon with real vertices, not a generated circle", async ({ campus }) => {
        campus.requireBoundary();
        const geometry = campus.boundary!;
        const positions = geometryPositions(geometry);

        expect(["Polygon", "MultiPolygon"]).toContain(geometry.type);
        expect(
            positions.length,
            `the boundary has ${positions.length} vertices. A parcel line traced from a county GIS layer has tens to hundreds; a very ` +
                "low count suggests a bounding box or a generated shape standing in for one",
        ).toBeGreaterThan(8);

        // A 50 m fallback circle is generated with evenly spaced points around
        // one centre, so every vertex sits at the same distance from it. A real
        // parcel does not. This catches a circle that happens to be large
        // enough to pass the area check.
        const distances = positions.map(([lon, lat]) => metresBetween(CAMPUS_CENTRE, { label: "vertex", latitude: lat, longitude: lon }));
        const spread = Math.max(...distances) - Math.min(...distances);
        expect(
            spread,
            "every vertex of this boundary is nearly equidistant from the campus centre, which is what a generated circle looks like and " +
                "what a surveyed parcel does not. The area check alone would not notice this",
        ).toBeGreaterThan(30);
    });

    test("the boundary payload the pin page renders agrees with the API", async ({ campus, page }) => {
        campus.requireBoundary();

        // `/dashboard/map/pin/<slug>/boundary/` is the richest view of this
        // subsystem and the one the map actually draws from - it carries the
        // provenance the external API does not.
        const response = await page.request.get(hrshRoutes.pinBoundary(campus.pin.slug));
        expect(response.status(), "the pin page's boundary endpoint did not answer").toBe(200);

        const payload = (await response.json()) as {
            pending?: boolean;
            refreshing?: boolean;
            boundaries?: { property?: { polygon?: unknown; source?: string }; building?: { polygon?: unknown; source?: string } };
        };
        const property = payload.boundaries?.property;

        expect(
            property?.polygon,
            `the map's own boundary payload has no property polygon (pending=${payload.pending}, refreshing=${payload.refreshing}). ` +
                "The API served one, so the two disagree - the map would draw nothing while the API says there is a parcel",
        ).toBeTruthy();
        expect(
            property?.source,
            `the property boundary's provenance is "${property?.source}". "circle" is the 50 m fallback, which means no provider ever ` +
                'answered; "place" is the resolved parcel. "pin" or "wiki" would mean somebody drew it by hand, which is not what this ' +
                "test is about",
        ).toBe("place");
    });

    test("the pin detail map draws the parcel boundary", async ({ campus, page }) => {
        campus.requireBoundary();

        await page.goto(`/dashboard/map/pin/${campus.pin.slug}/`);

        // The map is `.location-map.pin-map` wrapping `#pin-detail-map-wrapper`;
        // there is no `#pin-detail-map`.
        //
        // The boundary is **not** in `.leaflet-overlay-pane`, which is where
        // Leaflet puts vectors by default and where an earlier version of this
        // test looked. `map-annotations.ts` renders it into a custom pane
        // (`pane: "boundaryPane"`), so the overlay pane is legitimately empty
        // and that assertion reported "nothing was drawn" against a map that had
        // drawn it. Verified by enumerating every `.leaflet-pane` on the page:
        // one path, and not in the overlay pane.
        //
        // Matching any pane rather than naming boundaryPane, because which pane
        // a layer lives in is an implementation choice while "a vector is drawn"
        // is the claim.
        const drawn = page.locator("#pin-detail-map-wrapper .leaflet-pane path");
        await expect(
            drawn.first(),
            "no vector was drawn anywhere on the pin detail map. The boundary endpoint returns a polygon, so either the map never " +
                "fetched it or it failed to render it - open the trace and look at the boundary request",
        ).toBeAttached({ timeout: 30_000 });
    });

    test("every campus coordinate is inside the drawn boundary, not merely near it", async ({ campus }) => {
        campus.requireBoundary();

        // The same containment claim as hrsh-place-identity, made against the
        // geometry rather than against the app's refusal. Both are worth having:
        // this one fails when the polygon is wrong, that one fails when the
        // polygon is right but the resolution rule does not use it.
        const outside = INSIDE_BOUNDARY.filter((point) => !containsCoordinate(campus.boundary, point));

        expect(
            outside.map((point) => point.label),
            `${outside.length} of the ${INSIDE_BOUNDARY.length} campus coordinates are outside the polygon the app resolved.

` +
                "Before reading this as an application defect, check the premise: REData's authoritative county parcel for this " +
                `coordinate contains only ${COUNTY_PARCEL_COVERS.contains} of the ${COUNTY_PARCEL_COVERS.of} - ` +
                `${COUNTY_PARCEL_COVERS.missing.join(" and ")} are outside it too (measured). That parcel is 33 acres while the ` +
                "redevelopment site is reported at 156, so the campus spans several parcels and no single polygon can contain all " +
                "five. If these five are genuinely one place, the thing that unifies them is the access domain (Place.domain_root), " +
                "not one parcel outline - which is a different assertion from this one.",
        ).toEqual([]);
    });

    test("the boundary is stable across two reads", async ({ campus }) => {
        campus.requireBoundary();

        // Geometry that changes between requests means an unresolved vote or a
        // provider chain still running, and every downstream assertion in this
        // directory would then be testing a moving target.
        const again = await campus.api.json<{ boundary: { type: string; coordinates: unknown } | null }>("get", `pins/${campus.pin.slug}/`);
        const first = approximateAreaSqm(campus.boundary);
        const second = approximateAreaSqm(again.boundary);

        expect(
            Math.abs(first - second) / Math.max(first, 1),
            `the parcel area moved from ${Math.round(first).toLocaleString()} to ${Math.round(second).toLocaleString()} m² between two ` +
                "reads seconds apart. Either the provider chain is still running or a boundary vote is being recomputed per request",
        ).toBeLessThan(0.01);
    });
});
