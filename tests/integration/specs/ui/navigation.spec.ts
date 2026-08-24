/**
 * The application shell: navigation, the account menu, the HTMX dropdowns.
 *
 * The shell renders on every page, which cuts both ways - a fault here is a
 * fault everywhere, and it is also the thing nobody writes a test for because
 * it is "just the header". The notification bell is the most valuable target on
 * it: clicking it dispatches an event on `body`, an `hx-trigger` listening for
 * that event fetches a fragment, and the fragment is swapped in. That is the
 * exact chain every HTMX interaction in this application uses, in its smallest
 * possible form.
 */

import { expect, test } from "../../lib/fixtures.js";
import { withHtmxSwap } from "../../lib/htmx.js";
import { AppShell } from "../../lib/pages/app-shell.js";
import { appRoutes, shellFragmentRoutes } from "../../lib/routes.js";

test.describe("application shell", () => {
    test("the navigation renders for a signed-in user", async ({ page, account }) => {
        await page.goto(appRoutes.home);

        const shell = new AppShell(page);
        await expect(shell.nav).toBeVisible();
        await expect(shell.brand).toBeVisible();
        await shell.expectSignedInAs(account.username);
    });

    test("the account menu opens and offers its destinations", async ({ page }) => {
        await page.goto(appRoutes.home);

        const shell = new AppShell(page);
        await shell.openUserMenu();

        await expect(shell.userDropdown.locator("a[href]")).not.toHaveCount(0);
        await expect(shell.userDropdown.locator(".nav-dropdown-item--signout")).toBeVisible();
    });

    test("the notification dropdown loads over HTMX", async ({ page }) => {
        await page.goto(appRoutes.home);

        const shell = new AppShell(page);
        // Empty until something asks for it - `hx-trigger="notifOpen from:body"`.
        await expect(shell.notificationsPanel).toBeEmpty();

        await withHtmxSwap(page, () => shell.notificationsButton.click());

        await expect(shell.notificationsPanel, "the bell was clicked and nothing was fetched").not.toBeEmpty();
    });

    test("the fragments the shell loads on every page all answer", async ({ page }) => {
        await page.goto(appRoutes.home);

        // Each of these is fetched by an `hx-trigger`, not by a click, so a
        // failure is invisible: the badge just never fills in and the banner
        // never appears. Nobody reports that as a bug for months.
        const failures: string[] = [];
        for (const [name, path] of Object.entries(shellFragmentRoutes)) {
            const response = await page.request.get(path);
            if (response.status() >= 400) {
                failures.push(`${name} (${path}) -> ${response.status()}`);
            }
        }
        expect(failures, `shell fragments failing silently:\n  ${failures.join("\n  ")}`).toHaveLength(0);
    });

    test("the search dialog opens from the header", async ({ page }) => {
        await page.goto(appRoutes.home);

        const overlay = page.locator("#global-search-overlay");
        // A modal that is in the DOM but never opened is the most common way
        // this breaks, so it is asserted closed first and open afterwards.
        // It is a div with a class toggle rather than a <dialog>, so both the
        // class and the aria state are checked - a half-applied open leaves
        // the overlay visible to sighted users and hidden to a screen reader.
        await expect(overlay).toHaveAttribute("aria-hidden", "true");

        await new AppShell(page).searchButton.click();

        await expect(overlay).toHaveClass(/is-open/);
        await expect(overlay).toHaveAttribute("aria-hidden", "false");
        await expect(page.locator("#gs-input")).toBeFocused();
    });

    test("the theme the page starts in is applied before first paint", async ({ page }) => {
        await page.goto(appRoutes.home);

        // The inline script in `themes/base.html` stamps `data-theme` before
        // anything renders, specifically so a dark-mode user never sees a white
        // flash. If the attribute is missing, that script did not run.
        const theme = await page.locator("#html-root").getAttribute("data-theme");
        expect(theme, "no data-theme was stamped on the document").toMatch(/^(light|dark)$/);
    });
});

test.describe("narrow viewport", () => {
    // Not a separate browser project: the mobile drawer is a layout concern of
    // the same page, and running the whole UI suite on a phone profile to check
    // one component would be a poor trade. `--project=ui-mobile` exists for the
    // occasions when the whole suite is worth running that way.
    test.use({ viewport: { width: 390, height: 844 } });

    test("the mobile drawer opens", async ({ page }) => {
        await page.goto(appRoutes.home);

        const shell = new AppShell(page);
        await expect(shell.hamburger).toBeVisible();
        await shell.hamburger.click();

        await expect(shell.mobileDrawer).toBeVisible();
        await expect(shell.mobileDrawer.locator("a.app-nav-link").first()).toBeVisible();
    });

    test("the map is usable at phone width", async ({ page }) => {
        await page.goto(appRoutes.map);
        await expect(page.locator("#map")).toHaveClass(/leaflet-container/);

        // A page that scrolls sideways on a phone is the single most common
        // responsive regression, and it is invisible at desktop width.
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        expect(overflow, `the page overflows its viewport horizontally by ${overflow}px`).toBeLessThanOrEqual(1);
    });
});
