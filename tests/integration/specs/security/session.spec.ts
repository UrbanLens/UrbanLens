/**
 * Sessions, CSRF, and redirects after sign-in.
 *
 * A cookie that JavaScript can read, a POST that Django will accept without a
 * CSRF token, or a `next=` that lands on another origin are each a complete
 * account takeover in the presence of a single other bug. The controls: a
 * real sign-in with a token still works (the form is not broken), and a
 * same-origin `next` is still honoured (open-redirect refusals are not
 * "redirects are broken").
 *
 * Every test here uses a context of its own. The rest of the suite shares one
 * saved session per role, and Django's logout flushes that session
 * server-side.
 */

import { expect, test } from "../../lib/fixtures.js";
import { env } from "../../lib/env.js";
import { appRoutes, publicRoutes } from "../../lib/routes.js";
import { anonymousContext, expectNotServerError, header } from "../../lib/security.js";
import { LoginPage } from "../../lib/pages/login-page.js";

const isHttps = env.baseUrl.startsWith("https:");

test.describe("cookies", () => {
    test("the session cookie is not readable from JavaScript", async ({ page }) => {
        await page.goto(appRoutes.home);
        const cookies = await page.context().cookies();
        const session = cookies.find((cookie) => cookie.name === "sessionid");
        expect(session, "no session cookie was set for a signed-in page").toBeTruthy();
        expect(session?.httpOnly, "sessionid is not HttpOnly, so any XSS can steal the session").toBeTruthy();
        expect(session?.sameSite?.toLowerCase(), `sessionid SameSite is "${session?.sameSite}", which allows cross-site sends`).toMatch(/lax|strict/);
        if (isHttps) {
            expect(session?.secure, "sessionid is not Secure on an HTTPS deployment").toBeTruthy();
        }

        const fromJs = await page.evaluate(() => document.cookie);
        expect(fromJs, "document.cookie includes sessionid, so HttpOnly is not actually in effect").not.toMatch(/(?:^|;\s*)sessionid=/);
    });

    test("the CSRF cookie exists and is not HttpOnly (the frontend has to read it)", async ({ page }) => {
        await page.goto(appRoutes.home);
        const cookies = await page.context().cookies();
        const csrf = cookies.find((cookie) => cookie.name === "csrftoken");
        expect(csrf, "no CSRF cookie was set").toBeTruthy();
        expect(csrf?.httpOnly, "the CSRF cookie is HttpOnly, so HTMX cannot send X-CSRFToken").toBeFalsy();
        if (isHttps) {
            expect(csrf?.secure).toBeTruthy();
        }
        expect(csrf?.sameSite?.toLowerCase()).toMatch(/lax|strict/);
    });
});

test.describe("CSRF", () => {
    test("a login POST without a CSRF token is refused", async ({ browser, account }) => {
        const context = await anonymousContext(browser);
        try {
            const response = await context.request.post(publicRoutes.login, {
                form: { username: account.username, password: account.password },
                maxRedirects: 0,
            });
            await expectNotServerError(response, "login POST without CSRF");
            expect(
                response.status(),
                `a login POST with no CSRF token answered ${response.status()} and would have established a session`,
            ).toBe(403);
        } finally {
            await context.close();
        }
    });

    test("a login POST with a token from a different origin is refused", async ({ browser, account }) => {
        const context = await anonymousContext(browser);
        try {
            const page = await context.newPage();
            await page.goto(publicRoutes.login);
            const token = await page.locator('#password-login-form input[name="csrfmiddlewaretoken"]').inputValue();
            expect(token, "the login form has no CSRF token, so there is nothing to bind to an origin").toBeTruthy();

            const response = await context.request.post(publicRoutes.login, {
                form: { username: account.username, password: account.password, csrfmiddlewaretoken: token },
                headers: {
                    Origin: "https://evil.example",
                    Referer: "https://evil.example/login",
                },
                maxRedirects: 0,
            });
            await expectNotServerError(response, "cross-origin login POST");
            expect(
                [403, 400],
                `a login POST whose Origin is evil.example answered ${response.status()}`,
            ).toContain(response.status());
        } finally {
            await context.close();
        }
    });

    test("a same-origin login POST with a valid token still works", async ({ browser, account }) => {
        // The control: if the two tests above pass because *every* login POST
        // is refused, they are not testing CSRF.
        const context = await anonymousContext(browser);
        try {
            const page = await context.newPage();
            await new LoginPage(page).signIn(account.username, account.password);
            expect(new URL(page.url()).pathname, "a legitimate login did not reach the application").toContain("/dashboard/");
            const cookies = await context.cookies();
            expect(cookies.some((cookie) => cookie.name === "sessionid")).toBeTruthy();
        } finally {
            await context.close();
        }
    });

    test("logout is a POST, and GET does not end the session", async ({ browser, account }) => {
        const context = await anonymousContext(browser);
        try {
            const page = await context.newPage();
            await new LoginPage(page).signIn(account.username, account.password);

            const getLogout = await page.request.get("/accounts/logout/", { maxRedirects: 0 });
            // Django's LogoutView refuses GET. A 200 that logged the user out
            // would make every prefetch, prefetch-on-hover, or leaked <img>
            // a logout.
            if (getLogout.status() === 200 || (getLogout.status() >= 300 && getLogout.status() < 400)) {
                await page.goto(appRoutes.map);
                expect(
                    new URL(page.url()).pathname,
                    "GET /accounts/logout/ ended the session",
                ).not.toContain("/accounts/login");
            } else {
                expect([403, 405], `GET logout answered ${getLogout.status()}`).toContain(getLogout.status());
            }
        } finally {
            await context.close();
        }
    });
});

test.describe("open redirects", () => {
    test("a same-origin next is honoured, so refusals below are not 'redirects are broken'", async ({ browser, account }) => {
        const context = await anonymousContext(browser);
        try {
            const page = await context.newPage();
            await page.goto(`${publicRoutes.login}?next=/dashboard/trips/`);
            await new LoginPage(page).submitCredentials(account.username, account.password);
            // post-login may bounce through /accounts/post-login/; what matters
            // is that we end on trips, not the default map, and never off-site.
            await page.waitForURL((url) => url.pathname.includes("/dashboard/"), { timeout: 30_000 });
            expect(new URL(page.url()).hostname).toBe(env.host);
            expect(
                new URL(page.url()).pathname,
                `a safe next=/dashboard/trips/ was dropped; landed on ${page.url()}`,
            ).toContain("/dashboard/trips");
        } finally {
            await context.close();
        }
    });

    for (const next of [
        "https://evil.example/",
        "https://evil.example/%0d%0a",
        "//evil.example/",
        "/\\evil.example/",
        "https:evil.example",
        "http://127.0.0.1/",
        "javascript:void(0)",
    ]) {
        test(`next=${next} does not leave the deployment`, async ({ browser, account }) => {
            const context = await anonymousContext(browser);
            try {
                const page = await context.newPage();
                await page.goto(`${publicRoutes.login}?next=${encodeURIComponent(next)}`);
                await new LoginPage(page).submitCredentials(account.username, account.password);
                await page.waitForURL((url) => !url.pathname.startsWith("/accounts/login"), { timeout: 30_000 });
                const landed = new URL(page.url());
                expect(landed.hostname, `sign-in followed next=${next} to ${page.url()}`).toBe(env.host);
                expect(landed.protocol, `sign-in followed next=${next} onto ${landed.protocol}`).toMatch(/^https?:$/);
                expect(page.url(), `the final URL still contains the off-site target ${next}`).not.toContain("evil.example");
            } finally {
                await context.close();
            }
        });
    }
});

test.describe("authenticated responses are not publicly cacheable", () => {
    test("a signed-in HTML page is not Cache-Control: public", async ({ page }) => {
        const response = await page.goto(appRoutes.home);
        expect(response, "the home page did not navigate").toBeTruthy();
        const cache = header(response!, "cache-control").toLowerCase();
        expect(cache, `a signed-in page was marked publicly cacheable: "${cache}"`).not.toMatch(/\bpublic\b/);
        expect(cache, `a signed-in page allows shared caches to store it: "${cache}"`).not.toMatch(/\bs-maxage\s*=\s*[1-9]/);
    });

    test("an authenticated API response is not Cache-Control: public", async ({ api }) => {
        const response = await api.get("whoami/");
        expect(response.status()).toBe(200);
        const cache = header(response, "cache-control").toLowerCase();
        expect(cache, `whoami was marked publicly cacheable: "${cache}"`).not.toMatch(/\bpublic\b/);
    });
});
