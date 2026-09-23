/**
 * Where the pin page puts its external-data panels: a card of their own, or a tab in Regional Data or Location Data.
 * Placement is server-rendered, so no panel is fetched here and no upstream is spent.
 */

import { expect, test } from "../../lib/fixtures.js";
import { PinDetailPage } from "../../lib/pages/pin-detail-page.js";

const REGIONAL_TABS = ["Disasters", "Water", "Air Quality"];
const LOCATION_TABS = ["Historic Registers"];
const FORMER_CARD_IDS = ["hazard-history-section", "hydrology-section", "air-quality-section", "historic-registers-section"];

test.describe("pin detail - panel layout", () => {
    test("regional data are Regional Data tabs and historic registers a Location Data tab, never cards", async ({ page, api, guard }) => {
        // htmx logs each request this test aborts.
        guard.allow(/htmx:(sendAbort|afterRequest)/);
        const pin = await api.createPin();
        await page.route("**/*", (route) =>
            ["xhr", "fetch"].includes(route.request().resourceType()) ? route.abort("aborted") : route.continue(),
        );

        await new PinDetailPage(page).goto(pin.slug);

        const regional = page.locator("#pin-plugin-tabs-section > .card-tabs .pin-plugin-tab-btn span");
        const location = page.locator("#location-data-section > .card-tabs .pin-plugin-tab-btn span");
        for (const label of REGIONAL_TABS) {
            await expect(regional.filter({ hasText: new RegExp(`^\\s*${label}\\s*$`) }), `Regional Data has no ${label} tab`).toHaveCount(1);
        }
        for (const label of LOCATION_TABS) {
            await expect(location.filter({ hasText: new RegExp(`^\\s*${label}\\s*$`) }), `Location Data has no ${label} tab`).toHaveCount(1);
        }
        for (const id of FORMER_CARD_IDS) {
            await expect(page.locator(`#${id}`), `#${id} still renders as a card of its own`).toHaveCount(0);
        }
    });
});
