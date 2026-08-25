/**
 * Accessibility scans of the pages people actually spend time on.
 *
 * Run against the deployed page rather than a rendered template, which is the
 * point: axe sees the DOM after HTMX has swapped its fragments in and after
 * Leaflet has built its panes, and most of this application's interactive
 * surface only exists at that moment.
 *
 * Only `serious` and `critical` findings fail. Everything below that is
 * attached to the report - see `lib/a11y.ts` for why that line is drawn there.
 */

import { expectAccessible } from "../../lib/a11y.js";
import { test } from "../../lib/fixtures.js";
import { appRoutes, contentRoutes, publicRoutes } from "../../lib/routes.js";
import { PinDetailPage } from "../../lib/pages/pin-detail-page.js";

/** Pages worth scanning as a signed-in user, and anything to exclude on each. */
const SIGNED_IN_PAGES: Array<{ name: string; path: string; exclude?: string[] }> = [
    { name: "home", path: appRoutes.home },
    // Leaflet's own controls and panes are third-party markup this project does
    // not author, and scanning them reports the same findings on every map page
    // while saying nothing about this application's own accessibility.
    { name: "map", path: appRoutes.map, exclude: [".leaflet-container"] },
    { name: "trips", path: appRoutes.trips },
    { name: "memories", path: appRoutes.memories },
    { name: "organize", path: appRoutes.organize },
    { name: "settings", path: appRoutes.settings },
    { name: "profile", path: appRoutes.profile },
];

test.describe("accessibility", () => {
    test.describe("anonymous", () => {
        test.use({ storageState: { cookies: [], origins: [] } });

        test("the sign-in page", async ({ page }) => {
            // The one page every user must get through. A keyboard or screen
            // reader failure here locks somebody out of the product entirely.
            await page.goto(publicRoutes.login);
            await expectAccessible(page);
        });

        test("the FAQ", async ({ page }) => {
            await page.goto(contentRoutes.faq);
            await expectAccessible(page);
        });
    });

    for (const target of SIGNED_IN_PAGES) {
        test(`the ${target.name} page`, async ({ page }) => {
            await page.goto(target.path);
            await expectAccessible(page, { exclude: target.exclude });
        });
    }

    test("a pin's detail page", async ({ page, api }) => {
        const pin = await api.createPin();
        await new PinDetailPage(page).goto(pin.slug);
        await expectAccessible(page, { exclude: [".leaflet-container"] });
    });

    test("the account menu, once opened", async ({ page }) => {
        // Menus are frequently accessible while closed and not while open -
        // the state a scan of the page at rest never reaches.
        await page.goto(appRoutes.home);
        await page.locator("#nav-user-btn").click();
        await page.locator("#nav-dropdown").waitFor({ state: "visible" });
        await expectAccessible(page, { include: "#nav-user" });
    });
});
