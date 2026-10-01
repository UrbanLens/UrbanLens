/**
 * The map's saved-filter toolbar, end to end: a filter saved from the filter panel appears in the
 * toolbar, narrows both the map and the pin list when toggled on, and restores both when toggled off.
 *
 * The filter is on priority because the toolbar's optimistic client-side pass cannot judge priority;
 * whatever the map shows after the toggle is the server's filtered result applied, or nothing.
 */

import type { Page } from "@playwright/test";

import type { ApiClient, CreatedPin } from "../../lib/api-client.js";
import { resourceName } from "../../lib/env.js";
import { expect, test } from "../../lib/fixtures.js";
import { waitForHtmxSettled, withHtmxSwap } from "../../lib/htmx.js";
import { installLeafletMapCapture, markerLatLngs } from "../../lib/leaflet.js";
import { MapPage } from "../../lib/pages/map-page.js";
import { expectNoErrorToast } from "../../lib/toasts.js";

const MATCHING = 2;
const NON_MATCHING = 3;
/** ~330 m: far enough apart that no two seeded pins resolve to one property. */
const SPACING_DEGREES = 0.004;
const ZOOM = 15;
const SEED_ATTEMPTS = 3;
/** Stored coordinates may be rounded; well under {@link SPACING_DEGREES}. */
const COORDINATE_TOLERANCE = 1e-4;

interface SeededPin {
    name: string;
    lat: number;
    lng: number;
    matches: boolean;
}

interface Seed {
    center: { lat: number; lng: number };
    pins: SeededPin[];
}

/**
 * A row of pins at a random rural spot, the first {@link MATCHING} at priority 5.
 *
 * Randomised per run so a leftover pin from an earlier run cannot sit on the same coordinates; a
 * refusal ("already have a pin") re-seeds somewhere else.
 */
async function seedPins(api: ApiClient): Promise<Seed> {
    let lastError: unknown;
    for (let attempt = 0; attempt < SEED_ATTEMPTS; attempt += 1) {
        const base = { lat: 43.2 + Math.random() * 0.6, lng: -75.6 + Math.random() * 0.6 };
        try {
            const pins: SeededPin[] = [];
            for (let i = 0; i < MATCHING + NON_MATCHING; i += 1) {
                const matches = i < MATCHING;
                const created: CreatedPin = await api.createPin({
                    name: resourceName(`saved filter ${matches ? "match" : "other"} ${i}`),
                    latitude: Number(base.lat.toFixed(6)),
                    longitude: Number((base.lng + i * SPACING_DEGREES).toFixed(6)),
                });
                const detail = await api.json<{ latitude: number; longitude: number }>("patch", `pins/${created.slug}/`, { priority: matches ? 5 : 0 });
                pins.push({ name: created.name, lat: Number(detail.latitude), lng: Number(detail.longitude), matches });
            }
            const lngs = pins.map((pin) => pin.lng);
            return { center: { lat: base.lat, lng: (Math.min(...lngs) + Math.max(...lngs)) / 2 }, pins };
        } catch (error) {
            if (!String(error).includes("already have a pin")) throw error;
            lastError = error;
        }
    }
    throw lastError;
}

/** Which seeded pins the map currently holds a marker for. */
async function seededOnMap(page: Page, seed: Seed): Promise<string[]> {
    const markers = await markerLatLngs(page);
    return seed.pins
        .filter((pin) => markers.some((m) => Math.abs(m.lat - pin.lat) < COORDINATE_TOLERANCE && Math.abs(m.lng - pin.lng) < COORDINATE_TOLERANCE))
        .map((pin) => pin.name)
        .sort();
}

/** Markers anywhere within the seeded row's neighbourhood, seeded or not. */
async function markersNearSeed(page: Page, seed: Seed): Promise<number> {
    const lats = seed.pins.map((pin) => pin.lat);
    const lngs = seed.pins.map((pin) => pin.lng);
    const pad = SPACING_DEGREES;
    const markers = await markerLatLngs(page);
    return markers.filter(
        (m) => m.lat >= Math.min(...lats) - pad && m.lat <= Math.max(...lats) + pad && m.lng >= Math.min(...lngs) - pad && m.lng <= Math.max(...lngs) + pad,
    ).length;
}

/** Which seeded pins the pin list panel currently lists. */
async function seededInList(map: MapPage, seed: Seed): Promise<string[]> {
    const names = (await map.pinListBody.locator(".pin-list-name").allTextContents()).map((text) => text.trim());
    return seed.pins
        .filter((pin) => names.includes(pin.name))
        .map((pin) => pin.name)
        .sort();
}

async function listedCount(map: MapPage): Promise<number> {
    const text = await map.pinListBody.locator(".pin-list-count strong").textContent();
    return Number.parseInt(text ?? "", 10);
}

test.describe("map saved filters", () => {
    test("a saved filter shows in the toolbar, narrows the map and pin list, and restores both", async ({ page, api }) => {
        test.slow();

        const seed = await seedPins(api);
        const all = seed.pins.map((pin) => pin.name).sort();
        const matching = seed.pins
            .filter((pin) => pin.matches)
            .map((pin) => pin.name)
            .sort();
        const filterName = resourceName(`priority 5 ${Date.now()}`);

        await installLeafletMapCapture(page);
        // The onboarding tips arrive on a timer and can sit over the filter panel.
        await page.addLocatorHandler(page.locator("#map-onboarding .map-onboarding-card"), async (card) => {
            await card.locator(".map-onboarding-x").click();
        });
        const map = new MapPage(page);
        await map.goto({ ...seed.center, zoom: ZOOM });

        await expect.poll(() => seededOnMap(page, seed), { message: "every seeded pin is on the map before filtering", timeout: 30_000 }).toEqual(all);
        await map.openPinList();
        await expect.poll(() => seededInList(map, seed), { message: "every seeded pin is in the pin list before filtering" }).toEqual(all);
        const unfilteredNearby = await markersNearSeed(page, seed);
        const unfilteredListed = await listedCount(map);

        // 1. Save a priority >= 5 filter from the panel; it joins the toolbar without a reload.
        await page.keyboard.press("f");
        await expect(map.filterPanel).toHaveClass(/\bopen\b/);
        const scores = page.locator("#fp-acc-scores");
        if (!(await scores.evaluate((details) => (details as HTMLDetailsElement).open))) {
            await scores.locator("summary").click();
        }
        await withHtmxSwap(page, () => map.filterForm.locator('[data-ul-dual-range-slider]:has(input[name="min_priority"]) input[data-role="min"]').fill("5"));

        await page.locator("#fp-save-filter-btn").click();
        const dialog = page.locator("#save-filter-dialog");
        await expect(dialog).toBeVisible();
        await dialog.locator("#saved-filter-name").fill(filterName);
        await withHtmxSwap(page, () => dialog.getByRole("button", { name: "Save Filter" }).click());
        await expect(dialog).toBeHidden();
        await expectNoErrorToast(page);

        const listing = await api.json<{ results: Array<{ uuid: string; name: string; criteria: Record<string, unknown> }> }>("get", "saved-filters/", { page_size: 100 });
        const saved = listing.results.find((row) => row.name === filterName);
        expect(saved, "the dialog saved the filter").toBeTruthy();
        api.track("saved filter", saved!.uuid, () => api.delete(`saved-filters/${saved!.uuid}/`));
        expect(saved!.criteria.min_priority, "the saved criteria are the panel's priority floor").toBe(5);

        const toolbarButton = page.locator("#map-saved-filters-toolbar").getByRole("button", { name: `Toggle saved filter: ${filterName}` });
        await expect(toolbarButton).toBeVisible();
        await expect(toolbarButton).toHaveAttribute("data-filter-uuid", saved!.uuid);

        // Clear the panel so only the toolbar filter is in play.
        await page.locator("#filter-form .fp-reset").click();
        await waitForHtmxSettled(page);
        await expect.poll(() => seededOnMap(page, seed), { message: "clearing the panel restores every seeded pin on the map" }).toEqual(all);
        await expect.poll(() => seededInList(map, seed), { message: "clearing the panel restores every seeded pin in the list" }).toEqual(all);
        await page.locator("#filter-panel .fp-close").click();
        await expect(map.filterPanel).not.toHaveClass(/\bopen\b/);

        // 2. Toggle it on: map and list narrow to the matching seeded pins.
        await toolbarButton.click();
        await expect(toolbarButton).toHaveClass(/\bactive\b/);
        await expect
            .poll(() => seededOnMap(page, seed), { message: "with the filter on, the map holds exactly the matching seeded pins", timeout: 20_000 })
            .toEqual(matching);
        await expect.poll(() => seededInList(map, seed), { message: "with the filter on, the list holds exactly the matching seeded pins" }).toEqual(matching);
        await waitForHtmxSettled(page);
        const filteredNearby = await markersNearSeed(page, seed);
        expect(filteredNearby, "filtered map shows fewer pins near the seed, but some").toBeLessThan(unfilteredNearby);
        expect(filteredNearby).toBeGreaterThanOrEqual(MATCHING);
        const filteredListed = await listedCount(map);
        expect(filteredListed, "filtered list count drops, but not to zero").toBeLessThan(unfilteredListed);
        expect(filteredListed).toBeGreaterThanOrEqual(MATCHING);
        await expectNoErrorToast(page);

        // 3. Toggle it off: the full set returns to both.
        await toolbarButton.click();
        await expect(toolbarButton).not.toHaveClass(/\bactive\b/);
        await expect.poll(() => seededOnMap(page, seed), { message: "with the filter off, every seeded pin is back on the map", timeout: 20_000 }).toEqual(all);
        await expect.poll(() => seededInList(map, seed), { message: "with the filter off, every seeded pin is back in the list" }).toEqual(all);
        await waitForHtmxSettled(page);
        expect(await markersNearSeed(page, seed)).toBe(unfilteredNearby);
        await expect.poll(() => listedCount(map)).toBe(unfilteredListed);
    });
});
