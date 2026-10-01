/**
 * Opening a building's floorplan for the first time: `finishLoadingDocument()` seeds the building's
 * exterior walls from `_building_outline()` and autosaves them with no click beyond the Floorplan
 * link itself (floorplan-editor.ts `seedFromOutline`/`fresh` branch). Untested at every level per
 * scratchpad research `floorplan.json` - this file is the first to exercise it against a real page.
 */

import type { Page } from "@playwright/test";

import { expect, locationDataTest as test, readPin, skipUnlessLocationDataEnabled, waitForChildPins, type CampusFixture, type SyncPinRow } from "./fixtures.js";
import { approximateAreaSqm, BUILDING_COORDINATE, hrshRoutes, metresBetween, type Coordinate, type GeoJsonGeometry } from "../../lib/hrsh.js";
import { recordMetric } from "../../lib/metrics.js";
import { pinDetail } from "../../lib/routes.js";

skipUnlessLocationDataEnabled();

interface FloorplanWall {
    kind?: string | null;
}

interface FloorplanDocument {
    floors: Array<{ walls: FloorplanWall[] }>;
}

interface Candidate {
    child: SyncPinRow;
    boundary: GeoJsonGeometry;
}

function floorplanJsonPath(slug: string): string {
    return `${hrshRoutes.floorplan(slug)}json/`;
}

function floorplanSavePath(slug: string): string {
    return `${hrshRoutes.floorplan(slug)}save/`;
}

function coordOf(row: SyncPinRow): Coordinate {
    return { label: row.name, latitude: row.latitude, longitude: row.longitude };
}

async function readOutline(page: Page): Promise<Array<[number, number]>> {
    const raw = await page.locator("#floorplan-outline").textContent();
    return JSON.parse(raw ?? "[]") as Array<[number, number]>;
}

/** Segments seedFromOutline would actually draw - it skips a pair under 5cm apart (floorplan-editor.ts). */
function countSegments(outline: Array<[number, number]>): number {
    let count = 0;
    for (let index = 0; index < outline.length; index += 1) {
        const [aLat, aLng] = outline[index]!;
        const [bLat, bLng] = outline[(index + 1) % outline.length]!;
        if (metresBetween({ label: "a", latitude: aLat, longitude: aLng }, { label: "b", latitude: bLat, longitude: bLng }) >= 0.05) {
            count += 1;
        }
    }
    return count;
}

function outlineAreaSqm(outline: Array<[number, number]>): number {
    return approximateAreaSqm({ type: "Polygon", coordinates: [outline.map(([lat, lng]) => [lng, lat])] });
}

/**
 * Picks a building child pin whose floorplan has never been opened, so the "first open" seed can be
 * proven rather than assumed. Memoized per file (this project runs one worker) so every test in this
 * file agrees on which building it is, and a failure here is not repeated per test.
 */
let candidatePromise: Promise<Candidate> | null = null;

function pickCandidate(campus: CampusFixture, page: Page): Promise<Candidate> {
    candidatePromise ??= resolveCandidate(campus, page);
    return candidatePromise;
}

async function resolveCandidate(campus: CampusFixture, page: Page): Promise<Candidate> {
    const children = await waitForChildPins(campus, { min: 1 });
    if (children.length === 0) {
        throw new Error(
            "no child pins exist under the campus pin, so there is no building to test a floorplan for. hrsh-child-pins.spec.ts reports " +
                "a broken or empty auto-nest pipeline as its own finding; that must pass before this file can test anything.",
        );
    }

    const withBoundary = (await Promise.all(children.map(async (child) => ({ child, boundary: (await readPin(campus.api, child.slug)).boundary })))).filter(
        (entry): entry is Candidate => entry.boundary !== null,
    );
    if (withBoundary.length === 0) {
        throw new Error(
            `none of the ${children.length} building child pin(s) has its own boundary. _building_outline (controllers/floorplans.py) refuses to fall ` +
                'back to the parcel line, so no floorplan can be seeded until at least one has a BUILDING boundary - see hrsh-buildings.spec.ts ' +
                '"that building has its own geometry, distinct from the parcel".',
        );
    }

    const withStatus = await Promise.all(withBoundary.map(async (entry) => ({ ...entry, status: (await page.request.get(floorplanJsonPath(entry.child.slug))).status() })));
    const fresh = withStatus.filter((entry) => entry.status === 204);
    if (fresh.length === 0) {
        const seen = withStatus.map((entry) => `${entry.child.name} (${entry.child.slug}): HTTP ${entry.status}`).join("; ");
        throw new Error(
            `every building child pin with its own boundary already has a saved floorplan (${seen}), so none is fresh enough to prove the first-open ` +
                "seed. There is no endpoint or UI to delete a Floorplan row - controllers/floorplans.py registers only JSON/Save/Publish/Features " +
                "views, none of them DELETE. Set UL_E2E_HRSH_FRESH=1 to delete the campus pin and its children and start over.",
        );
    }

    return fresh.reduce((nearest, entry) => (metresBetween(BUILDING_COORDINATE, coordOf(entry.child)) < metresBetween(BUILDING_COORDINATE, coordOf(nearest.child)) ? entry : nearest));
}

test.describe("Hudson River State Hospital - floorplan walls drawn automatically", () => {
    test("clicking a building's Floorplan link draws its exterior walls with no further interaction, and autosaves them", async ({ campus, page }) => {
        campus.requireBoundary();
        const candidate = await pickCandidate(campus, page);

        const openedAt = Date.now();
        await page.goto(pinDetail(candidate.child.slug));
        const savePosted = page
            .waitForResponse((response) => new URL(response.url()).pathname === floorplanSavePath(candidate.child.slug) && response.request().method() === "POST", { timeout: 20_000 })
            .catch(() => null);

        await page.getByRole("link", { name: /floorplan/i }).click();

        await expect(page.locator("#floorplan-map .leaflet-container, #floorplan-map.leaflet-container"), "the floorplan map never initialised").toBeAttached({ timeout: 30_000 });

        const walls = page.locator(".floorplan-wall");
        await expect(
            walls.first(),
            "no wall was drawn with zero interaction beyond the Floorplan click - finishLoadingDocument's fresh branch should have run seedFromOutline",
        ).toBeAttached({ timeout: 30_000 });

        const wallsDrawnAt = Date.now();
        const wallCount = await walls.count();
        recordMetric({ name: "hrsh.floorplan.seconds_to_walls_drawn", value: Math.round((wallsDrawnAt - openedAt) / 1000), unit: "s" });
        recordMetric({ name: "hrsh.floorplan.wall_count", value: wallCount, unit: "count" });

        await expect(page.locator("#floorplan-empty"), "#floorplan-empty is still shown; updateEmptyState hides it only once the floor has walls").toBeHidden();
        await expect(page.locator("#floorplan-map"), "#floorplan-map never gained has-plan, which is toggled only once an exterior wall exists").toHaveClass(/(?:^|\s)has-plan(?:\s|$)/);

        const outline = await readOutline(page);
        const expectedSegments = countSegments(outline);
        expect(wallCount, `drew ${wallCount} wall(s) for an outline of ${outline.length} point(s) (${expectedSegments} non-degenerate segment(s))`).toBeGreaterThanOrEqual(3);
        expect(
            Math.abs(wallCount - expectedSegments),
            `wall count (${wallCount}) should track the outline's own segment count (${expectedSegments}), give or take a dropped sub-5cm segment`,
        ).toBeLessThanOrEqual(Math.max(1, Math.ceil(expectedSegments * 0.1)));

        const saveResponse = await savePosted;
        expect(
            saveResponse,
            `no POST to ${floorplanSavePath(candidate.child.slug)} was observed within 20s of the walls rendering - a fresh seed marks the document ` +
                "dirty and calls queueAutosave(), so a save should follow with no user action",
        ).not.toBeNull();
        expect(saveResponse!.ok(), `the autosave POST answered HTTP ${saveResponse!.status()}`).toBe(true);
    });

    test("the auto-seeded walls trace the building's own footprint, not the parcel", async ({ campus, page }) => {
        campus.requireBoundary();
        const candidate = await pickCandidate(campus, page);
        await page.goto(hrshRoutes.floorplan(candidate.child.slug));

        const outline = await readOutline(page);
        expect(outline.length, "no outline was handed to the editor for this building").toBeGreaterThanOrEqual(3);

        const latitudes = outline.map(([lat]) => lat);
        const longitudes = outline.map(([, lng]) => lng);
        const span = Math.max(
            metresBetween({ label: "n", latitude: Math.min(...latitudes), longitude: longitudes[0]! }, { label: "s", latitude: Math.max(...latitudes), longitude: longitudes[0]! }),
            metresBetween({ label: "w", latitude: latitudes[0]!, longitude: Math.min(...longitudes) }, { label: "e", latitude: latitudes[0]!, longitude: Math.max(...longitudes) }),
        );
        expect(span, `the outline spans about ${Math.round(span)} m at its widest, which reads as the parcel rather than a single building`).toBeLessThan(300);

        const outlineArea = outlineAreaSqm(outline);
        const boundaryArea = approximateAreaSqm(candidate.boundary);
        const ratio = boundaryArea > 0 ? outlineArea / boundaryArea : Number.POSITIVE_INFINITY;
        expect(
            ratio,
            `the floorplan outline covers about ${Math.round(outlineArea).toLocaleString()} m², against ${Math.round(boundaryArea).toLocaleString()} m² for ` +
                `${candidate.child.name}'s own boundary from GET pins/{slug}/ (ratio ${ratio.toFixed(2)}) - both should describe the same building footprint`,
        ).toBeGreaterThan(0.2);
        expect(ratio, `same mismatch as above, in the other direction (ratio ${ratio.toFixed(2)})`).toBeLessThan(5);
    });

    test("the seeded walls are saved as exterior walls, and survive a reload", async ({ campus, page }) => {
        campus.requireBoundary();
        const candidate = await pickCandidate(campus, page);

        const response = await page.request.get(floorplanJsonPath(candidate.child.slug));
        expect(
            response.status(),
            `GET ${floorplanJsonPath(candidate.child.slug)} answered HTTP ${response.status()}; expected 200 - the earlier test's autosave should have written a Floorplan row`,
        ).toBe(200);

        const document = (await response.json()) as FloorplanDocument;
        const walls = document.floors[0]?.walls ?? [];
        expect(walls.length, "the saved document has no walls").toBeGreaterThanOrEqual(3);

        const notExterior = walls.filter((wall) => wall.kind !== "exterior").map((wall) => wall.kind ?? "null");
        expect(notExterior, `seedFromOutline always writes kind="exterior"; these saved walls did not: ${notExterior.join(", ") || "none"}`).toEqual([]);

        await page.goto(hrshRoutes.floorplan(candidate.child.slug));
        await expect(page.locator(".floorplan-wall"), "reloading the floorplan page should redraw the same saved walls, not start over from empty").toHaveCount(walls.length, { timeout: 30_000 });
    });
});
