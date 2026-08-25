/**
 * Signing in, signing out, and being refused.
 *
 * Every test here works in a context of its own, with its own fresh sign-in.
 * That is not fastidiousness: the rest of the suite shares one saved session,
 * and Django's logout flushes the session *server-side* - so a sign-out
 * performed against the shared cookie would sign out every test running in
 * parallel, and they would fail with "redirected to login" for reasons nothing
 * in their own code explains.
 */

import { expect, test } from "../../lib/fixtures.js";
import { env } from "../../lib/env.js";
import { AppShell } from "../../lib/pages/app-shell.js";
import { LoginPage } from "../../lib/pages/login-page.js";
import { appRoutes, publicRoutes } from "../../lib/routes.js";

/** A context with no session at all, and its own cookie jar. */
async function anonymousPage(browser: import("@playwright/test").Browser) {
    const context = await browser.newContext({
        baseURL: env.baseUrl,
        storageState: { cookies: [], origins: [] },
        ignoreHTTPSErrors: env.ignoreHttpsErrors,
    });
    return { context, page: await context.newPage() };
}

test.describe("authentication", () => {
    test("valid credentials establish a session and land in the application", async ({ browser, account }) => {
        const { context, page } = await anonymousPage(browser);
        try {
            await new LoginPage(page).signIn(account.username, account.password);

            await new AppShell(page).expectSignedInAs(account.username);
            expect(new URL(page.url()).pathname).toContain("/dashboard/");

            const cookies = await context.cookies();
            expect(cookies.some((cookie) => cookie.name === "sessionid")).toBeTruthy();
        } finally {
            await context.close();
        }
    });

    test("signing out ends the session", async ({ browser, account }) => {
        const { context, page } = await anonymousPage(browser);
        try {
            await new LoginPage(page).signIn(account.username, account.password);

            const shell = new AppShell(page);
            await shell.openUserMenu();
            // A POST form, not a link - Django's LogoutView refuses GET.
            await shell.userDropdown.locator(".nav-dropdown-item--signout").click();

            // Whatever it redirects to, the session must be gone - so this is
            // asserted by asking for a protected page rather than by reading
            // the current URL.
            await page.goto(appRoutes.map);
            expect(new URL(page.url()).pathname, "a protected page was served after signing out").toContain("/accounts/login");
        } finally {
            await context.close();
        }
    });

    test("a protected page redirects an anonymous visitor to sign in, and remembers where they were going", async ({ browser }) => {
        const { context, page } = await anonymousPage(browser);
        try {
            await page.goto(appRoutes.trips);
            const url = new URL(page.url());

            expect(url.pathname).toContain("/accounts/login");
            // Losing `next` sends every deep link to the map after sign-in,
            // which is the difference between a shared link working and not.
            expect(url.searchParams.get("next"), "the sign-in redirect dropped the requested destination").toContain("/dashboard/trips");
        } finally {
            await context.close();
        }
    });

    test("a wrong password is refused with a visible message", async ({ browser, account }) => {
        // Exactly one failed attempt, once per run. `CustomLoginView` counts
        // failures per identifier *and* per client IP, so a spec that looped
        // here would lock this suite out of the deployment it is testing.
        const { context, page } = await anonymousPage(browser);
        try {
            const login = new LoginPage(page);
            await login.goto();
            await login.username.fill(account.username);
            await login.password.fill("definitely-not-the-password");
            await login.submit.click();

            await expect(login.errors.first(), "a rejected sign-in showed the user nothing").toBeVisible();
            expect(new URL(page.url()).pathname).toContain("/accounts/login");
        } finally {
            await context.close();
        }
    });

    test("the sign-in form carries CSRF protection", async ({ browser }) => {
        const { context, page } = await anonymousPage(browser);
        try {
            await page.goto(publicRoutes.login);
            const token = page.locator('#password-login-form input[name="csrfmiddlewaretoken"]');
            await expect(token, "the sign-in form has no CSRF token").toBeAttached();
            expect(await token.inputValue()).not.toBe("");
        } finally {
            await context.close();
        }
    });
});
