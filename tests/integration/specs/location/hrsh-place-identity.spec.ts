/**
 * Five coordinates on one property have to mean one property.
 *
 * This is the foundational claim the rest of `specs/location/` rests on. A user
 * who drops a pin anywhere on the Hudson River State Hospital campus has pinned
 * *the campus*, and two users who drop pins 400 m apart on it are looking at the
 * same place - which is what makes a shared wiki, a shared boundary and shared
 * building data coherent rather than a coincidence of proximity.
 *
 * The app's own answer to "is this the same property" is not a field; it is a
 * refusal. `services.pins.pin_creation` enforces one root pin per property, and
 * says so in words that distinguish the two cases:
 *
 * - `"You already have a pin on this property."` - place-based, and the thing
 *   these tests are about.
 * - `"You already have a pin at this location."` - the exact-coordinate unique
 *   constraint, which is a different and much weaker statement.
 *
 * Asserting on *which* refusal comes back is the sharpest available evidence,
 * and better than inferring identity from a shared wiki slug: two pins can share
 * a wiki for reasons that have nothing to do with the parcel.
 *
 * **The refusal is conditional on geometry already existing.** The rule fires
 * only `if new_parent is None and location.place_id`, and `place_id` is set only
 * once a provider has actually supplied a parcel polygon. On virgin ground every
 * pin is created happily and nothing is refused - so these tests skip rather
 * than fail when the parcel never arrived, and `hrsh-boundary.spec.ts` reports
 * that absence as the finding it is.
 */

import { expect, locationDataTest as test, skipUnlessLocationDataEnabled } from "./fixtures.js";
import { approximateAreaSqm, containsCoordinate, EXPECTED_PARCEL_AREA_SQM, INSIDE_BOUNDARY, MEASURED_PARCEL_AREA_SQM, OUTSIDE_BOUNDARY, REPORTED_PROJECT_ACREAGE } from "../../lib/hrsh.js";

skipUnlessLocationDataEnabled();

test.describe("Hudson River State Hospital - one property, five coordinates", () => {
    test("the parcel contains every coordinate that is on the campus", async ({ campus }) => {
        campus.requireBoundary();

        const missing = INSIDE_BOUNDARY.filter((point) => !containsCoordinate(campus.boundary, point));

        expect(
            missing.map((point) => `${point.label} (${point.latitude}, ${point.longitude})`),
            "these campus coordinates fall outside the parcel the app resolved, so a user pinning there gets a different place - and a " +
                "different wiki - from a user pinning a few hundred metres away on the same grounds",
        ).toEqual([]);
    });

    test("the parcel excludes coordinates that are not on the campus", async ({ campus }) => {
        campus.requireBoundary();

        const wronglyInside = OUTSIDE_BOUNDARY.filter((point) => containsCoordinate(campus.boundary, point));

        expect(
            wronglyInside.map((point) => `${point.label} (${point.latitude}, ${point.longitude})`),
            "the resolved parcel swallows coordinates that are not on this property. Too large is worse than absent: the place domain is " +
                "what grants wiki access, so an oversized parcel hands out other people's places. The nearest of these is only ~565 m from " +
                "the campus centre, so an approximately-right boundary does not pass this",
        ).toEqual([]);
    });

    test("the parcel is the size of a campus, not of a building or a survey zone", async ({ campus }) => {
        campus.requireBoundary();
        const area = approximateAreaSqm(campus.boundary);

        expect(
            area,
            `the resolved boundary covers about ${Math.round(area).toLocaleString()} m². REData measures this parcel at ` +
                `${MEASURED_PARCEL_AREA_SQM.toLocaleString()} m² (33 acres) - note that is the *parcel*, not the ~${REPORTED_PROJECT_ACREAGE}-acre ` +
                "redevelopment site the press describes, which spans several. The range is deliberately wide: it is catching a single " +
                "building footprint chosen instead of the parcel, or the ~1,040-acre CRIS archaeological sensitivity zone",
        ).toBeGreaterThan(EXPECTED_PARCEL_AREA_SQM.min);
        expect(area).toBeLessThan(EXPECTED_PARCEL_AREA_SQM.max);
    });

    test("a second pin elsewhere on the campus is refused as the same property", async ({ campus }) => {
        campus.requireBoundary();

        // Deliberately the coordinate furthest from the one already pinned. If
        // anything distance-based were doing this work instead of the polygon,
        // this is the pair it would get wrong.
        const elsewhere = INSIDE_BOUNDARY[4]!;
        const response = await campus.api.post("pins/", {
            name: "e2e hrsh same-property probe",
            latitude: elsewhere.latitude,
            longitude: elsewhere.longitude,
            name_is_user_provided: true,
        });
        const body = await response.text();
        if (response.ok()) {
            const created = JSON.parse(body) as { slug?: string };
            if (created.slug) {
                await campus.api.delete(`pins/${created.slug}/`);
            }
        }

        expect(
            response.status(),
            `a second pin ${Math.round(campus.metresFromFirstPin(elsewhere))} m away on the same parcel returned ${response.status()}; ` +
                `expected 400. Body: ${body.slice(0, 300)}`,
        ).toBe(400);
        expect(
            body,
            'the refusal came back, but not as a property-level one. "at this location" is the exact-coordinate unique constraint, which ' +
                'would also refuse two pins on unrelated parcels sharing a coordinate. Only "on this property" means the parcel polygon ' +
                "did the work",
        ).toContain("already have a pin on this property");
    });

    test("every remaining campus coordinate is refused the same way", async ({ campus }) => {
        campus.requireBoundary();

        // The first is already pinned and the fifth is covered above. Run as one
        // test because each is the same claim: a failure listing all of them at
        // once says more than three separate reds.
        const remaining = [INSIDE_BOUNDARY[1]!, INSIDE_BOUNDARY[2]!, INSIDE_BOUNDARY[3]!];
        const unexpected: string[] = [];

        for (const point of remaining) {
            const response = await campus.api.post("pins/", {
                name: `e2e hrsh same-property probe ${point.label}`,
                latitude: point.latitude,
                longitude: point.longitude,
                name_is_user_provided: true,
            });
            const body = await response.text();
            if (response.status() === 400 && body.includes("already have a pin on this property")) {
                continue;
            }
            unexpected.push(`${point.label}: HTTP ${response.status()} ${body.slice(0, 160)}`);
            // A 201 really did create a pin, and leaving it behind would poison
            // every later run of this file.
            if (response.ok()) {
                const created = JSON.parse(body) as { slug?: string };
                if (created.slug) {
                    await campus.api.delete(`pins/${created.slug}/`);
                }
            }
        }

        expect(
            unexpected,
            "these campus coordinates were not recognised as the property that is already pinned - each is a place the app believes is " +
                "somewhere else, which means its own wiki, its own boundary and its own building data for one site",
        ).toEqual([]);
    });

    test("a pin off the campus is accepted, because it is a different property", async ({ campus }) => {
        campus.requireBoundary();

        // Without this the refusals above prove nothing: an endpoint refusing
        // every second pin for any reason would satisfy all of them.
        const refused: string[] = [];
        let accepted = 0;

        for (const point of OUTSIDE_BOUNDARY) {
            const response = await campus.api.post("pins/", {
                name: `e2e hrsh off-campus probe ${point.label}`,
                latitude: point.latitude,
                longitude: point.longitude,
                name_is_user_provided: true,
            });
            const body = await response.text();
            if (response.ok()) {
                accepted += 1;
                const created = JSON.parse(body) as { slug?: string };
                if (created.slug) {
                    await campus.api.delete(`pins/${created.slug}/`);
                }
            } else {
                refused.push(`${point.label}: HTTP ${response.status()} ${body.slice(0, 160)}`);
            }
        }

        expect(
            refused,
            `${refused.length} of ${OUTSIDE_BOUNDARY.length} off-campus coordinates were refused as already-pinned. They are 565 m to ` +
                "1.9 km from the campus centre and are not on this property; refusing them means the resolved property is far larger than " +
                "the parcel, which grants access to places the user has not earned",
        ).toEqual([]);
        expect(accepted).toBe(OUTSIDE_BOUNDARY.length);
    });

    test("the campus pin reports a location slug that addresses its shared wiki", async ({ campus }) => {
        // Not gated on the boundary: `location_slug` is populated at creation
        // regardless, and its absence would be a different (and worse) defect.
        expect(
            campus.pin.location_slug,
            "the pin has no location_slug, so there is no identifier to reach its community wiki with - every wiki route is keyed by it",
        ).toBeTruthy();
    });
});
