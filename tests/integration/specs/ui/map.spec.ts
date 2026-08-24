/**
 * The map, and the HTMX exchange behind its filter.
 *
 * Two distinct things are checked and they fail for different reasons. That
 * Leaflet initialised is a *bundle* assertion: `#map` is in the HTML whether or
 * not any script ran, so a grey rectangle where the map should be is invisible
 * to any check that only looks for the element. That the filter round-trips is
 * an *HTMX* assertion, and HTMX is how most of this application updates itself
 * - if its exchange is broken here it is broken everywhere.
 */

import { expect, test } from "../../lib/fixtures.js";
import { withHtmxSwap } from "../../lib/htmx.js";
import { MapPage } from "../../lib/pages/map-page.js";
import { expectNoErrorToast } from "../../lib/toasts.js";

test.describe("map", () => {
    test("Leaflet takes over the container", async ({ page }) => {
        const map = new MapPage(page);
        await map.goto();

        // Beyond the container class: a map with no tile pane has not laid
        // anything out, which is what a half-initialised Leaflet looks like.
        await expect(map.map.locator(".leaflet-tile-pane")).toBeAttached();
    });

    test("a pin created over the API is in the map's feed", async ({ page, api }) => {
        const pin = await api.createPin();

        const map = new MapPage(page);
        await map.goto();

        const feed = await map.fetchPins();
        expect(feed.some((entry) => entry.slug === pin.slug || entry.uuid === pin.uuid), `pin ${pin.slug} is missing from the map feed`).toBeTruthy();
    });

    test("the filter form completes an HTMX round trip", async ({ page, api }) => {
        const pin = await api.createPin();

        const map = new MapPage(page);
        await map.goto();

        // The panel is collapsed until asked for; "F" is the documented shortcut
        // and exercises the same handler the toolbar button calls.
        await page.keyboard.press("f");
        await expect(map.searchInput).toBeVisible();

        // `hx-trigger` debounces this input by 600ms, so the swap is waited for
        // explicitly rather than assumed to have happened by the next line.
        await withHtmxSwap(page, async () => {
            await map.searchInput.fill(pin.name);
        });

        // A failed swap surfaces as an error toast (base.html wires
        // `htmx:responseError` globally), which is a far clearer signal than
        // the absence of a DOM change.
        await expectNoErrorToast(page);
        await map.expectMapReady();
    });

    test("the pin list panel opens and lists pins", async ({ page, api }) => {
        await api.createPin();

        const map = new MapPage(page);
        await map.goto();
        await map.openPinList();

        // The panel loads its rows over HTMX from the same filter state as the
        // map, so an empty body on an account that has pins means the two have
        // drifted apart.
        await expect(map.pinListBody).not.toBeEmpty();
    });

    test("the basemap catalogue answers", async ({ page }) => {
        await page.goto("/dashboard/map/");
        // The layer switcher is built from this. A 500 here leaves the map on
        // whatever it defaulted to, with no visible error.
        const response = await page.request.get("/dashboard/map/basemap-tiles/sources/");
        expect(response.status()).toBe(200);
    });
});
