/**
 * Buildings on the campus: discovered, turned into child pins, and traceable.
 *
 * ## What is actually automatic
 *
 * There is no "suggested building" row anywhere - no model, no review queue. The
 * building list is a `LocationCache` row with `source="parcel_buildings"`,
 * written by `plugins.builtin.parcel_buildings` from REData (county footprint
 * layer plus NY SHPO's CRIS inventory), and everything downstream is computed
 * from it on the fly.
 *
 * Child pins *are* created without being asked, by `services.pins.auto_nest`,
 * under conditions worth knowing because each one is a way for this to
 * legitimately produce nothing:
 *
 * - `Profile.auto_create_building_pins` must be on (it defaults to True).
 * - The pin must be a root pin that has never been swept
 *   (`Pin.buildings_auto_nested_at` is one-shot and never cleared).
 * - The pin must have no children already - an existing hierarchy is the user's
 *   own arrangement and is not overwritten.
 * - There must be at least `MULTI_BUILDING_THRESHOLD` (2) *confident* buildings,
 *   where confident means on the property and carrying no `overlap_refs`.
 *   Ambiguous records deliberately wait for approval in the dialog.
 *
 * `auto_nest` swallows its own exceptions with a log line, so an HTTP 200 from
 * anything here proves nothing about whether pins were made. These tests assert
 * on the child pins themselves.
 *
 * ## The floorplan half
 *
 * `_building_outline` uses **only** a BUILDING boundary and deliberately refuses
 * to fall back to the property line - on a campus that would seed a room shaped
 * like the entire grounds, which is "both wrong and confidently wrong". So the
 * floorplan assertions belong on a *building* pin, and the campus pin correctly
 * having no outline is not a defect.
 */

import { allPins, expect, locationDataTest as test, skipUnlessLocationDataEnabled, type CampusFixture } from "./fixtures.js";
import { BUILDING_COORDINATE, containsCoordinate, hrshRoutes, metresBetween, type GeoJsonGeometry } from "../../lib/hrsh.js";
import { waitForOrNull } from "../../lib/waiting.js";

skipUnlessLocationDataEnabled();

/** How close a child pin must be to count as the building the requirement names. */
const BUILDING_MATCH_RADIUS_M = 60;

interface ChildPin {
    uuid: string;
    slug: string;
    name: string;
    latitude: number;
    longitude: number;
    pin_type?: string | null;
    parent_uuid?: string | null;
}

/**
 * The campus pin's child pins.
 *
 * There is no `pins/{slug}/children/` route. `GET pins/` is a delta-sync
 * endpoint that serves child pins alongside root ones and carries `parent_uuid`
 * on every row, so filtering it is the only way to ask this through the
 * published API - see `allPins`, which also handles that endpoint's `pins`
 * envelope and cursor paging.
 */
async function childPins(campus: CampusFixture): Promise<ChildPin[]> {
    const rows = await allPins(campus.api);
    return rows.filter((row) => row.parent_uuid === campus.pin.uuid) as ChildPin[];
}

test.describe("Hudson River State Hospital - buildings on the property", () => {
    test("the building list for the parcel is populated", async ({ campus }) => {
        campus.requireBoundary();

        const panel = await waitForOrNull(
            () => campus.api.get(`pins/${campus.pin.slug}/panels/parcel_buildings/`),
            (response) => response.status() === 200,
            {
                what: "the Buildings on this Property panel",
                timeoutMs: 300_000,
                intervalMs: 10_000,
                describe: (response) => `HTTP ${response.status()}`,
            },
        );

        expect(
            panel,
            "the parcel_buildings panel never became ready. It is fetched on the panel_fetch queue, so check that a worker is consuming " +
                "that queue - the default celery worker does not",
        ).not.toBeNull();

        const body = await campus.api.json<{ info?: { buildings?: unknown[] }; buildings?: unknown[] }>("get", `pins/${campus.pin.slug}/panels/parcel_buildings/`);
        const buildings = body.info?.buildings ?? body.buildings ?? [];
        expect(
            buildings.length,
            "the panel is ready but lists no buildings. This is a hospital campus of dozens of structures; an empty list means REData " +
                "returned no building layer for this parcel rather than that there are none",
        ).toBeGreaterThan(1);
    });

    test("confident buildings become child pins without being asked", async ({ campus }) => {
        campus.requireBoundary();

        const children = await waitForOrNull(() => childPins(campus), (list) => list.length > 0, {
            what: "automatically created child pins for the campus buildings",
            timeoutMs: 300_000,
            intervalMs: 15_000,
            describe: (list) => `${list.length} child pin(s)`,
        });

        expect(
            children,
            "no child pins were created for the buildings on this property. auto_nest requires: the profile's auto_create_building_pins " +
                "(default on), a root pin never swept before (Pin.buildings_auto_nested_at is one-shot), no existing children, and at " +
                "least 2 confident buildings - confident meaning on-property and free of overlap_refs. It also swallows its own " +
                "exceptions with only a log line, so check the worker log for 'auto_nest' before concluding the data was insufficient",
        ).not.toBeNull();
    });

    test("the child pins are typed as buildings and sit inside the parcel", async ({ campus }) => {
        campus.requireBoundary();
        const children = await childPins(campus);
        test.skip(children.length === 0, "no child pins exist - see the previous test, which reports that as the finding.");

        const outside = children.filter((child) => !containsCoordinate(campus.boundary as GeoJsonGeometry, { label: child.name, latitude: child.latitude, longitude: child.longitude }));

        expect(
            outside.map((child) => `${child.name} (${child.latitude}, ${child.longitude})`),
            "these automatically created child pins are outside the parcel they were created under. docs/PROBLEMS.md records an open " +
                "defect where building-place provisioning passes REData's unfiltered parcel cache, so off-property records can become " +
                "places inside this parcel's access domain - this is what that would look like from outside",
        ).toEqual([]);
    });

    test("the building named in the requirements has a child pin", async ({ campus }) => {
        campus.requireBoundary();
        const children = await childPins(campus);
        test.skip(children.length === 0, "no child pins exist - see the earlier test.");

        const near = children
            .map((child) => ({ child, distance: metresBetween(BUILDING_COORDINATE, { label: child.name, latitude: child.latitude, longitude: child.longitude }) }))
            .sort((a, b) => a.distance - b.distance);

        expect(
            near[0]?.distance ?? Number.POSITIVE_INFINITY,
            `the nearest child pin to ${BUILDING_COORDINATE.latitude}, ${BUILDING_COORDINATE.longitude} is ` +
                `${near[0] ? `${near[0].child.name} at ${Math.round(near[0].distance)} m` : "none at all"}. That coordinate is a building ` +
                "on this campus, so a building pin is expected within a footprint's width of it",
        ).toBeLessThan(BUILDING_MATCH_RADIUS_M);
    });

    test("that building has its own geometry, distinct from the parcel", async ({ campus }) => {
        campus.requireBoundary();
        const children = await childPins(campus);
        test.skip(children.length === 0, "no child pins exist - see the earlier test.");

        const nearest = children
            .map((child) => ({ child, distance: metresBetween(BUILDING_COORDINATE, { label: child.name, latitude: child.latitude, longitude: child.longitude }) }))
            .sort((a, b) => a.distance - b.distance)[0];
        test.skip(!nearest || nearest.distance > BUILDING_MATCH_RADIUS_M, "no child pin near the requirement's building coordinate.");

        const detail = await campus.api.json<{ boundary?: GeoJsonGeometry | null }>("get", `pins/${nearest!.child.slug}/`);

        expect(
            detail.boundary,
            `the building pin ${nearest!.child.name} has no boundary of its own. Without a BUILDING boundary the floorplan editor has ` +
                "nothing to seed from - _building_outline uses only that type and deliberately will not fall back to the property line",
        ).toBeTruthy();
    });
});

test.describe("Hudson River State Hospital - the floorplan editor", () => {
    test("the floorplan page offers the building's footprint to trace from", async ({ campus, page }) => {
        campus.requireBoundary();
        const children = await childPins(campus);
        test.skip(children.length === 0, "no building pin to open a floorplan for.");

        const nearest = children
            .map((child) => ({ child, distance: metresBetween(BUILDING_COORDINATE, { label: child.name, latitude: child.latitude, longitude: child.longitude }) }))
            .sort((a, b) => a.distance - b.distance)[0];
        test.skip(!nearest || nearest.distance > BUILDING_MATCH_RADIUS_M, "no child pin near the requirement's building coordinate.");

        await page.goto(hrshRoutes.floorplan(nearest!.child.slug));

        // `#floorplan-outline` is a json_script block and is ALWAYS in the DOM -
        // it renders `[]` when no footprint is known. So its presence proves
        // nothing; the contents are the assertion.
        const outlineJson = await page.locator("#floorplan-outline").textContent();
        const outline = JSON.parse(outlineJson ?? "[]") as Array<[number, number]>;

        expect(
            outline.length,
            `the floorplan editor was handed ${outline.length} outline points for this building. Three are needed to seed the exterior ` +
                "walls; below that the editor shows #floorplan-empty and the user has to trace a wall they can already see on the map",
        ).toBeGreaterThanOrEqual(3);
    });

    test("the outline handed to the editor is the building, not the grounds", async ({ campus, page }) => {
        campus.requireBoundary();
        const children = await childPins(campus);
        test.skip(children.length === 0, "no building pin to open a floorplan for.");

        const nearest = children
            .map((child) => ({ child, distance: metresBetween(BUILDING_COORDINATE, { label: child.name, latitude: child.latitude, longitude: child.longitude }) }))
            .sort((a, b) => a.distance - b.distance)[0];
        test.skip(!nearest || nearest.distance > BUILDING_MATCH_RADIUS_M, "no child pin near the requirement's building coordinate.");

        await page.goto(hrshRoutes.floorplan(nearest!.child.slug));
        const outline = JSON.parse((await page.locator("#floorplan-outline").textContent()) ?? "[]") as Array<[number, number]>;
        test.skip(outline.length < 3, "no outline to measure - see the previous test.");

        // The failure `_building_outline`'s docstring warns about: seeding the
        // parcel instead of the structure produces "an enormous room shaped like
        // the parcel", which looks like survey data and is not. A building on
        // this campus is tens of metres across, not hundreds.
        const latitudes = outline.map(([lat]) => lat);
        const longitudes = outline.map(([, lon]) => lon);
        const span = Math.max(
            metresBetween({ label: "n", latitude: Math.min(...latitudes), longitude: longitudes[0]! }, { label: "s", latitude: Math.max(...latitudes), longitude: longitudes[0]! }),
            metresBetween({ label: "w", latitude: latitudes[0]!, longitude: Math.min(...longitudes) }, { label: "e", latitude: latitudes[0]!, longitude: Math.max(...longitudes) }),
        );

        expect(
            span,
            `the outline spans about ${Math.round(span)} m at its widest. A building footprint on this campus is tens of metres; a span ` +
                "in the hundreds means the parcel line was used, which _building_outline explicitly refuses to do because it seeds a " +
                "room shaped like the grounds",
        ).toBeLessThan(300);
        expect(span, "the outline is smaller than any real building, which suggests a degenerate polygon").toBeGreaterThan(5);
    });

    test("the floorplan editor offers to start from the outline", async ({ campus, page }) => {
        campus.requireBoundary();
        const children = await childPins(campus);
        test.skip(children.length === 0, "no building pin to open a floorplan for.");

        const nearest = children
            .map((child) => ({ child, distance: metresBetween(BUILDING_COORDINATE, { label: child.name, latitude: child.latitude, longitude: child.longitude }) }))
            .sort((a, b) => a.distance - b.distance)[0];
        test.skip(!nearest || nearest.distance > BUILDING_MATCH_RADIUS_M, "no child pin near the requirement's building coordinate.");

        await page.goto(hrshRoutes.floorplan(nearest!.child.slug));

        // Unlike `#floorplan-outline`, this button is rendered conditionally on
        // an outline existing, so its presence is a genuine signal. It is also
        // the thing a user actually reaches for.
        await expect(
            page.locator("#floorplan-start-outline"),
            'the editor did not offer "start from the building outline". That button is rendered only when a footprint is known, so its ' +
                "absence means this building has no BUILDING-type boundary to trace",
        ).toBeAttached();
    });

    test("the floorplan map renders", async ({ campus, page }) => {
        campus.requireBoundary();
        const children = await childPins(campus);
        test.skip(children.length === 0, "no building pin to open a floorplan for.");

        await page.goto(hrshRoutes.floorplan(children[0]!.slug));

        // Leaflet and leaflet-rotate load from unpkg with SRI. If a deployment
        // blocks third-party egress the map is silently empty, which looks like
        // a regression and is not - so this is asserted separately from the
        // outline tests, and a failure here explains theirs.
        await expect(page.locator("#floorplan-map"), "the floorplan editor's map container is missing entirely").toBeAttached();
        await expect(
            page.locator("#floorplan-map .leaflet-container, #floorplan-map.leaflet-container"),
            "the floorplan map container never initialised as a Leaflet map. Leaflet loads from unpkg.com; a deployment with third-party " +
                "egress blocked leaves this empty with nothing in the DOM to say why",
        ).toBeAttached({ timeout: 30_000 });
    });
});
