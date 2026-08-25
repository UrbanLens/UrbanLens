/**
 * The pin detail page.
 *
 * The busiest page in the application, and the one that reaches the most
 * services: it resolves boundaries, asks REData about the parcel, looks for a
 * wiki, loads plugin-contributed enrichment panels, and renders its own map.
 * Most of that is asynchronous and degrades quietly, so the assertions here are
 * about the page's *own* content being right and nothing on it throwing -
 * panel contents depend on what the outside world knows about a coordinate and
 * are not something a test can pin down.
 */

import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import { PinDetailPage, type PinTab } from "../../lib/pages/pin-detail-page.js";
import { pinDetail } from "../../lib/routes.js";

const TABS: PinTab[] = ["overview", "visits", "photos", "article", "comments", "history"];

test.describe("pin detail", () => {
    test("renders the pin the API created", async ({ page, api }) => {
        const name = resourceName("detail");
        const pin = await api.createPin({ name });

        const detail = new PinDetailPage(page);
        await detail.goto(pin.slug);

        await expect(detail.hero).toContainText(name);
    });

    test("every section tab opens", async ({ page, api }) => {
        const pin = await api.createPin();
        const detail = new PinDetailPage(page);
        await detail.goto(pin.slug);

        for (const tab of TABS) {
            // A tab whose panel is missing switches the button's state and
            // shows nothing, so both halves are checked.
            await detail.openTab(tab);
            await expect(detail.content).toBeVisible();
        }
    });

    test("renders its own map", async ({ page, api }) => {
        const pin = await api.createPin();
        await new PinDetailPage(page).goto(pin.slug);

        // The detail map is a second Leaflet instance with different setup from
        // the main map, and has broken independently of it before.
        await expect(page.locator(".leaflet-container").first()).toBeVisible();
    });

    test("a slug that was never issued 404s", async ({ page }) => {
        const response = await page.goto(pinDetail("a-slug-that-was-never-issued-91b2c"));
        expect(response?.status()).toBe(404);
    });

    ifSecondaryAccount()("another account's pin is not reachable by guessing its URL", async ({ page, secondaryApi }) => {
        // Created by the secondary account, requested by the primary. A pin is
        // one user's private record, and the answer must be indistinguishable
        // from one for a pin that never existed - otherwise a slug becomes an
        // oracle for what other people have pinned.
        const theirs = await secondaryApi.createPin({ name: resourceName("someone else's pin") });

        const response = await page.goto(pinDetail(theirs.slug));
        expect(response?.status(), "one account could open another account's pin").toBe(404);
    });

    test("a deleted pin stops rendering", async ({ page, api }) => {
        const pin = await api.createPin();
        await new PinDetailPage(page).goto(pin.slug);

        await api.delete(`pins/${pin.slug}/`);

        const response = await page.goto(pinDetail(pin.slug));
        expect(response?.status(), "a deleted pin's page is still being served").toBe(404);
    });
});
