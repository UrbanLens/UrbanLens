/**
 * Child pins for every building on the campus, created without being asked. `hrsh-buildings.spec.ts`
 * covers the auto-nest mechanism itself; this file asks whether it reaches every building the
 * requirement names, whether it ever double-pins one, and whether the resulting pins work as their
 * own pages and as markers on the parent's map.
 */

import { childPins, expect, locationDataTest as test, skipUnlessLocationDataEnabled, waitForChildPins, type CampusFixture, type SyncPinRow } from "./fixtures.js";
import { containsCoordinate, metresBetween, type Coordinate } from "../../lib/hrsh.js";
import { recordMetric } from "../../lib/metrics.js";
import { pinDetail } from "../../lib/routes.js";
import { waitForOrNull } from "../../lib/waiting.js";

skipUnlessLocationDataEnabled();

/** One row of the "Buildings on this Property" panel (`ParcelBuildingsPanelSource.api_payload`). */
interface BuildingRow {
    name: string;
    building_number: string;
    latitude: number | null;
    longitude: number | null;
    has_geometry: boolean;
    child_pin_uuid: string | null;
    child_pin_name: string | null;
    can_create: boolean;
}

/** The app's own "same building" match radius (`site_scope.BUILDING_MATCH_METERS`); two child pins this close are one building, twice. */
const DUPLICATE_RADIUS_M = 15;

function coordOf(row: SyncPinRow): Coordinate {
    return { label: row.name, latitude: row.latitude, longitude: row.longitude };
}

/** Polls `panels/parcel_buildings/` until it answers 200, and returns its buildings, or null if it never does. */
async function waitForBuildingsPanel(campus: CampusFixture): Promise<BuildingRow[] | null> {
    const ready = await waitForOrNull(() => campus.api.get(`pins/${campus.pin.slug}/panels/parcel_buildings/`), (response) => response.status() === 200, {
        what: "the Buildings on this Property panel",
        timeoutMs: 300_000,
        intervalMs: 10_000,
        describe: (response) => `HTTP ${response.status()}`,
    });
    if (!ready) {
        return null;
    }
    const body = await campus.api.json<{ buildings?: BuildingRow[] }>("get", `pins/${campus.pin.slug}/panels/parcel_buildings/`);
    return body.buildings ?? [];
}

test.describe("Hudson River State Hospital - child pins for every building", () => {
    test("every building on the property has been given a child pin", async ({ campus }) => {
        campus.requireBoundary();

        const buildings = await waitForBuildingsPanel(campus);
        expect(buildings, "the parcel_buildings panel never became ready - it runs on the panel_fetch queue; check that a worker is consuming it").not.toBeNull();
        expect(buildings!.length, "the panel lists no buildings for this campus, so nothing here could be pinned").toBeGreaterThan(0);

        // Gives auto_nest a chance to run and records how long the first one took (hrsh.child_pins.seconds_to_min/count).
        await waitForChildPins(campus, { min: 1 });

        const settled = (await waitForBuildingsPanel(campus)) ?? buildings!;
        const pinned = settled.filter((row) => row.child_pin_uuid);
        const unpinned = settled.filter((row) => !row.child_pin_uuid);

        recordMetric({ name: "hrsh.parcel_buildings.count", value: settled.length, unit: "count" });
        recordMetric({ name: "hrsh.parcel_buildings.unpinned_count", value: unpinned.length, unit: "count" });

        expect(
            unpinned.map((row) => row.name || row.building_number || "(unnamed building)"),
            `${unpinned.length} of ${settled.length} buildings on the property have no child pin (${pinned.length} do). The requirement is ` +
                'every building; auto_nest only pins "confident" buildings (confident_buildings() in parcel_buildings.py drops any record ' +
                'carrying overlap_refs), leaving an ambiguous one for the manual "Organize this property?" dialog instead of an automatic pin',
        ).toEqual([]);
    });

    test("no two child pins are duplicates of the same building", async ({ campus }) => {
        const children = await childPins(campus);
        test.skip(children.length === 0, "no child pins exist - reported as a failure in the previous test.");

        const duplicates: string[] = [];
        for (let i = 0; i < children.length; i += 1) {
            for (let j = i + 1; j < children.length; j += 1) {
                const distance = metresBetween(coordOf(children[i]!), coordOf(children[j]!));
                if (distance < DUPLICATE_RADIUS_M) {
                    duplicates.push(`"${children[i]!.name}" and "${children[j]!.name}" (${Math.round(distance)} m apart)`);
                }
            }
        }
        recordMetric({ name: "hrsh.child_pins.duplicate_count", value: duplicates.length, unit: "count" });

        expect(
            duplicates,
            `these child pin pairs sit within ${DUPLICATE_RADIUS_M} m of each other, the app's own "same building" match radius - each pair ` +
                `looks like two pins for one building: ${duplicates.join("; ") || "none"}`,
        ).toEqual([]);
    });

    test("every child pin sits inside the parcel and is typed as a building", async ({ campus }) => {
        campus.requireBoundary();
        const children = await childPins(campus);
        test.skip(children.length === 0, "no child pins exist - reported as a failure in the earlier test.");

        const outside = children.filter((child) => !containsCoordinate(campus.boundary, coordOf(child)));
        expect(outside.map((child) => `${child.name} (${child.latitude}, ${child.longitude})`), "these child pins were auto-created but sit outside the parcel they were nested under").toEqual([]);

        const wrongType = children.filter((child) => child.pin_type !== "building");
        expect(
            wrongType.map((child) => `${child.name}: pin_type=${child.pin_type ?? "null"}`),
            'every auto-created building child should carry pin_type="building" (PinType.BUILDING); one of these does not, which means ' +
                "create_building_pins is not the code path that produced it",
        ).toEqual([]);
    });

    test("every child pin's own private pin detail page answers 200", async ({ campus, page }) => {
        const children = await childPins(campus);
        test.skip(children.length === 0, "no child pins exist - reported as a failure in the earlier test.");

        const statuses = await Promise.all(children.map(async (child) => ({ child, status: (await page.request.get(pinDetail(child.slug))).status() })));
        const broken = statuses.filter((entry) => entry.status !== 200);

        expect(
            broken.map((entry) => `${entry.child.name} (${entry.child.slug}): HTTP ${entry.status}`),
            "every auto-created building is an ordinary Pin row, so its private detail page should be reachable exactly like any other pin's",
        ).toEqual([]);
    });

    test("a child pin's detail page renders its map and a Floorplan link", async ({ campus, page }) => {
        const children = await childPins(campus);
        test.skip(children.length === 0, "no child pins exist - reported as a failure in the earlier test.");

        // One representative page: the previous test already swept every child pin's status.
        const sample = [...children].sort((a, b) => a.slug.localeCompare(b.slug))[0]!;
        await page.goto(pinDetail(sample.slug));

        await expect(page.locator("#pin-detail-map-wrapper"), `${sample.name}'s detail page rendered no map`).toBeAttached();
        await expect(page.getByRole("link", { name: /floorplan/i }), `${sample.name}'s detail page has no Floorplan link`).toBeAttached();
    });

    test("the parent's private pin detail page map shows the child pins", async ({ campus, page }) => {
        const children = await childPins(campus);
        test.skip(children.length === 0, "no child pins exist - reported as a failure in the earlier test.");

        await page.goto(pinDetail(campus.pin.slug));
        await expect(
            page.locator(".detail-pin-list-item").first(),
            "the parent pin's detail-pin list never rendered anything; map-annotations.ts fetches detailPinsJsonUrl unconditionally on load",
        ).toBeAttached({ timeout: 20_000 });

        const missing = (
            await Promise.all(children.map(async (child) => ({ child, present: (await page.locator(`.detail-pin-list-item[data-uuid="${child.uuid}"]`).count()) > 0 })))
        ).filter((entry) => !entry.present);

        expect(
            missing.map((entry) => `${entry.child.name} (${entry.child.uuid})`),
            `${missing.length} of ${children.length} child pins do not appear on the parent's own map/list. A parcel-scope pin defaults ` +
                "include_children on (is_site_scope), so pin.descendants() should list every one of them",
        ).toEqual([]);
    });
});
