/**
 * Every page the application links to still renders.
 *
 * The cheapest test in the suite and, in practice, the one that catches the
 * most: a template that references a context key a view stopped providing, a
 * `{% url %}` for a route that moved, an N+1 that turned into a timeout, a
 * migration that has not been applied. None of those need a clever assertion -
 * they need somebody to open the page.
 *
 * The signed-in sweep discovers its targets from the rendered navigation rather
 * than from a list in this file, so a page added later is covered without
 * anyone remembering to come back here.
 */

import { expect, test } from "../../lib/fixtures.js";
import { AppShell } from "../../lib/pages/app-shell.js";
import { contentRoutes, optionalRoutes, publicRoutes, signedInSweep } from "../../lib/routes.js";

test.describe("public pages", () => {
    // These must render for somebody who is not signed in, so the saved session
    // is deliberately discarded for this block. Without that, "the login page
    // renders" would be tested against a session that redirects away from it.
    test.use({ storageState: { cookies: [], origins: [] } });

    test("the landing page renders", async ({ page }) => {
        const response = await page.goto(publicRoutes.index);
        expect(response?.status()).toBeLessThan(400);
        await expect(page.locator("body")).toBeVisible();
    });

    test("the sign-in form renders", async ({ page }) => {
        await page.goto(publicRoutes.login);
        await expect(page.locator("#id_username")).toBeVisible();
        await expect(page.locator("#id_password")).toBeVisible();
        await expect(page.locator("#password-login-form button[type=submit]")).toBeEnabled();
    });

    test("the sign-up page renders or is deliberately restricted", async ({ page }) => {
        const response = await page.goto(publicRoutes.signup);
        // 403 is a real answer here: an instance with invite-only sign-up
        // renders `signup_restricted.html` with that status.
        expect([200, 403]).toContain(response?.status());
    });

    for (const [name, path] of Object.entries(contentRoutes)) {
        test(`the ${name} page renders anonymously`, async ({ page }) => {
            const response = await page.goto(path);
            expect(response?.status(), `${path} answered ${response?.status()}`).toBeLessThan(400);
        });
    }

    test("an unrouted path renders the styled 404 rather than a stack trace", async ({ page }) => {
        const response = await page.goto("/this-route-does-not-exist-91b2c");
        expect(response?.status()).toBe(404);
        // The catch-all renders a real template. A bare Django error page here
        // means DEBUG is on, which on a deployed instance is a finding in itself.
        await expect(page.locator("body")).not.toContainText("DisallowedHost");
        await expect(page.locator("body")).not.toContainText("Traceback");
    });
});

test.describe("signed-in pages", () => {
    for (const { name, path } of signedInSweep) {
        test(`${name} renders for a signed-in user`, async ({ page }) => {
            const response = await page.goto(path);
            expect(response?.status(), `${path} answered ${response?.status()}`).toBeLessThan(400);

            // Landing back on the sign-in form is the failure this sweep is
            // most likely to hit and the easiest to misread: the page "loaded",
            // it was just the wrong one.
            expect(new URL(page.url()).pathname, `${path} redirected to the sign-in page`).not.toContain("/accounts/login");
            await expect(new AppShell(page).nav).toBeVisible();
        });
    }

    for (const target of optionalRoutes) {
        test(`${target.name} either renders or is deliberately unavailable`, async ({ page }) => {
            const response = await page.goto(target.path);
            const status = response?.status() ?? 0;
            const landedOnLogin = new URL(page.url()).pathname.includes("/accounts/login");

            // A 404, a 403 or a bounce to sign-in from one of these is the gate
            // doing its job, not a fault - so it is reported rather than failed.
            // What is worth checking is the other half: when the deployment
            // *does* serve the page, it has to actually render.
            if (status === 404 || status === 403 || landedOnLogin) {
                test.info().annotations.push({ type: "unavailable", description: `${target.name} is gated off here (${target.gate}).` });
                return;
            }

            expect(status, `${target.path} answered ${status}`).toBeLessThan(400);
            await expect(new AppShell(page).nav).toBeVisible();
        });
    }

    test("every destination in the navigation resolves", async ({ page }) => {
        const shell = new AppShell(page);
        await page.goto("/dashboard/home/");
        const targets = await shell.navigationTargets();

        expect(targets.length, "the navigation rendered no links at all").toBeGreaterThan(3);

        for (const target of targets) {
            const response = await page.request.get(target.href);
            expect(response.status(), `"${target.label}" (${target.href}) answered ${response.status()}`).toBeLessThan(400);
        }
    });
});
