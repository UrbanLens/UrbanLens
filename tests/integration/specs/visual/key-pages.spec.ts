/**
 * Screenshot comparison, opt-in.
 *
 * Off by default (`UL_E2E_VISUAL=1` registers this project) for a reason worth
 * stating rather than rediscovering: these run against a live deployment whose
 * data changes, whose map tiles arrive from a third party at their own pace,
 * and whose relative timestamps ("2 days ago") differ from one day to the next.
 * A baseline taken under those conditions goes stale on its own, and a suite
 * that cries wolf on every run stops being read.
 *
 * What makes them worth having anyway is the class of regression nothing else
 * catches: a stylesheet that failed to build, a layout that collapsed at one
 * breakpoint, a dark-mode palette applied to half a page. Run them deliberately
 * - before and after a front-end change - rather than continuously.
 *
 * Baselines live next to this file and are committed. Refresh them with
 * `UL_E2E_VISUAL=1 npx playwright test --project=visual --update-snapshots`,
 * and read the diff before accepting it.
 */

import { expect, test } from "../../lib/fixtures.js";
import { appRoutes, publicRoutes } from "../../lib/routes.js";

/** Anything that legitimately differs between two runs of the same page. */
const MASKED = [
    // Relative timestamps, unread counts, and anything else that moves on its
    // own. Masking is preferable to a loose threshold: a masked region is
    // explicitly not being checked, whereas a threshold quietly stops checking
    // everything by a little.
    ".nav-notif-badge",
    "#notif-badge-wrap",
    "#msg-badge-wrap",
    ".leaflet-container",
];

/** Comparison settings that tolerate font hinting without tolerating a layout change. */
const COMPARISON = {
    maxDiffPixelRatio: 0.01,
    animations: "disabled" as const,
};

test.describe("visual", () => {
    test.describe("anonymous", () => {
        test.use({ storageState: { cookies: [], origins: [] } });

        test("the sign-in page", async ({ page }) => {
            await page.goto(publicRoutes.login);
            await expect(page).toHaveScreenshot("login.png", COMPARISON);
        });
    });

    test("the home page", async ({ page }) => {
        await page.goto(appRoutes.home);
        await expect(page).toHaveScreenshot("home.png", {
            ...COMPARISON,
            mask: MASKED.map((selector) => page.locator(selector)),
        });
    });

    test("the settings page", async ({ page }) => {
        await page.goto(appRoutes.settings);
        await expect(page).toHaveScreenshot("settings.png", {
            ...COMPARISON,
            mask: MASKED.map((selector) => page.locator(selector)),
        });
    });

    test("the home page in dark mode", async ({ page }) => {
        // The palette is applied by an inline script stamping `data-theme`, so
        // the two themes are genuinely different renders rather than one
        // stylesheet with a media query - and have diverged before.
        await page.goto(appRoutes.home);
        await page.locator("#html-root").evaluate((element) => element.setAttribute("data-theme", "dark"));
        await expect(page).toHaveScreenshot("home-dark.png", {
            ...COMPARISON,
            mask: MASKED.map((selector) => page.locator(selector)),
        });
    });

    test("the home page at phone width", async ({ page }) => {
        await page.setViewportSize({ width: 390, height: 844 });
        await page.goto(appRoutes.home);
        await expect(page).toHaveScreenshot("home-mobile.png", {
            ...COMPARISON,
            mask: MASKED.map((selector) => page.locator(selector)),
        });
    });
});
