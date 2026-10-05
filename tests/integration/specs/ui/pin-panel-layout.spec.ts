/**
 * Where the pin page puts its external-data panels: a card of their own, or a tab in Regional Data, Location Data or
 * Property Records. Placement is server-rendered, so no panel is fetched here and no upstream is spent.
 */

import { expect, test } from "../../lib/fixtures.js";
import { PinDetailPage } from "../../lib/pages/pin-detail-page.js";

const REGIONAL_TABS = ["Disasters", "Water", "Air Quality"];
const LOCATION_TABS = ["Site Conditions"];
const PROPERTY_TABS = ["Overview", "Parcel", "Historic Preservation"];
const RETIRED_PROPERTY_TABS = ["Historic Registers", "NY Historic Preservation (CRIS)"];
const FORMER_CARD_IDS = [
    "hazard-history-section",
    "hydrology-section",
    "air-quality-section",
    "historic-registers-section",
    "cris-building-section",
    "site-conditions-section",
];

function exactly(label: string): RegExp {
    return new RegExp(`^\\s*${label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*$`);
}

test.describe("pin detail - panel layout", () => {
    test("regional, location and property data are tabs of their cards, never cards of their own", async ({ page, api, guard }) => {
        // htmx logs each request this test aborts.
        guard.allow(/htmx:(sendAbort|afterRequest)/);
        const pin = await api.createPin();
        await page.route("**/*", (route) =>
            ["xhr", "fetch"].includes(route.request().resourceType()) ? route.abort("aborted") : route.continue(),
        );

        await new PinDetailPage(page).goto(pin.slug);

        const regional = page.locator("#pin-plugin-tabs-section > .card-tabs .pin-plugin-tab-btn span");
        const location = page.locator("#location-data-section > .card-tabs .pin-plugin-tab-btn span");
        const property = page.locator("#property-records-section > .card-tabs .pin-plugin-tab-btn span");
        for (const label of REGIONAL_TABS) {
            await expect(regional.filter({ hasText: exactly(label) }), `Regional Data has no ${label} tab`).toHaveCount(1);
        }
        for (const label of LOCATION_TABS) {
            await expect(location.filter({ hasText: exactly(label) }), `Location Data has no ${label} tab`).toHaveCount(1);
        }
        for (const label of PROPERTY_TABS) {
            await expect(property.filter({ hasText: exactly(label) }), `Property Records has no ${label} tab`).toHaveCount(1);
        }
        for (const label of RETIRED_PROPERTY_TABS) {
            await expect(property.filter({ hasText: exactly(label) }), `Property Records still has a ${label} tab`).toHaveCount(0);
        }
        for (const id of FORMER_CARD_IDS) {
            await expect(page.locator(`#${id}`), `#${id} still renders as a card of its own`).toHaveCount(0);
        }
    });
});
