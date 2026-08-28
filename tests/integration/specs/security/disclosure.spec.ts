/**
 * Information the deployment must not volunteer.
 *
 * Stack traces, source files, git metadata, environment files, credential
 * material in HTML, and debug toolbars are reconnaissance that a later
 * change can re-enable with one setting. Each probe has a control: a
 * well-known public path still answers, so a 404 here is "this file is not
 * served" rather than "the host is down".
 */

import { expect, test } from "../../lib/fixtures.js";
import { appRoutes, contentRoutes, publicRoutes, staffRoutes } from "../../lib/routes.js";
import {
    anonymousContext,
    DIRECTORY_PROBES,
    expectNoDebugLeak,
    expectNotServerError,
    header,
    SENSITIVE_PATHS,
} from "../../lib/security.js";

test.describe("error pages do not debug the deployment", () => {
    test("a 404 is a branded page, not a Django debug page", async ({ request }) => {
        const response = await request.get("/this-path-does-not-exist-91b2c/");
        expect(response.status(), "a missing path was not a 404").toBe(404);
        const body = await response.text();
        await expectNoDebugLeak(body, "the 404 page");
        expect(body.length, "the 404 page was empty, so there is no branded page to compare leaks against").toBeGreaterThan(50);
    });

    test("an authenticated 404 is equally quiet", async ({ page }) => {
        const response = await page.goto("/dashboard/this-path-does-not-exist-91b2c/");
        expect(response?.status()).toBe(404);
        await expectNoDebugLeak(await page.content(), "an authenticated 404");
    });
});

test.describe("source, secrets and debug surfaces are not served", () => {
    test("a public page still answers, so later 404s are not a down host", async ({ request }) => {
        const response = await request.get(contentRoutes.privacy);
        expect(response.status(), "the privacy page did not load; the probes below would 404 for the wrong reason").toBe(200);
    });

    test("well-known sensitive paths do not serve their contents", async ({ request }) => {
        const leaked: string[] = [];
        for (const probe of SENSITIVE_PATHS) {
            const response = await request.get(probe.path, { maxRedirects: 0 });
            await expectNotServerError(response, probe.name);
            if (response.status() !== 200) {
                continue;
            }
            const body = await response.text();
            if (probe.leak.test(body)) {
                leaked.push(`${probe.name} (${probe.path}) answered 200 with its real contents`);
            }
        }
        expect(leaked, `the deployment served sensitive files:\n  ${leaked.join("\n  ")}`).toHaveLength(0);
    });

    test("directories do not list their files", async ({ request }) => {
        const listed: string[] = [];
        for (const path of DIRECTORY_PROBES) {
            const response = await request.get(path, { maxRedirects: 0 });
            if (response.status() !== 200) {
                continue;
            }
            const body = await response.text();
            if (/<(?:title|h1)>[^<]*index of/i.test(body) || /\[core\]/.test(body)) {
                listed.push(`${path} looks like a directory listing`);
            }
        }
        expect(listed, `directory listings are enabled:\n  ${listed.join("\n  ")}`).toHaveLength(0);
    });
});

test.describe("credentials never appear in HTML or JSON the browser is given", () => {
    test("a signed-in page does not embed the account password or API key", async ({ page, account }) => {
        await page.goto(appRoutes.settings);
        const html = await page.content();
        expect(html, "the settings page contains the account password").not.toContain(account.password);
        if (account.apiKey) {
            expect(html, "the settings page contains the raw API key").not.toContain(account.apiKey);
        }
        expect(html, "the settings page contains a raw ulk_ key").not.toMatch(/ulk_[A-Za-z0-9]{12,}/);
        await expectNoDebugLeak(html, "the settings page");
    });

    test("whoami is only uuid and slug", async ({ api }) => {
        const body = await api.json<Record<string, unknown>>("get", "whoami/");
        expect(Object.keys(body).sort(), "whoami grew a field that is not uuid/slug").toEqual(["slug", "uuid"]);
    });

    test("the auth session document does not echo the raw key", async ({ api, account }) => {
        const response = await api.get("auth/session/");
        expect(response.status(), `auth/session/ answered ${response.status()}`).toBe(200);
        const body = await response.text();
        if (account.apiKey) {
            expect(body, "auth/session/ echoed the raw API key").not.toContain(account.apiKey);
        }
        expect(body, "auth/session/ contains a password").not.toMatch(/"password"\s*:\s*"/);
        await expectNoDebugLeak(body, "auth/session/");
    });

    test("account settings JSON does not include secrets", async ({ api, account }) => {
        const response = await api.get("settings/");
        if (response.status() === 403) {
            test.skip(true, "This key cannot read settings; nothing to inspect.");
        }
        expect(response.status()).toBe(200);
        const body = await response.text();
        if (account.apiKey) {
            expect(body).not.toContain(account.apiKey);
        }
        expect(body).not.toContain(account.password);
        expect(body, "settings JSON includes totp/secret material").not.toMatch(/otpauth:\/\//i);
        await expectNoDebugLeak(body, "GET settings/");
    });
});

test.describe("staff surfaces stay closed to ordinary accounts", () => {
    test("site-admin is forbidden to a non-staff session", async ({ page, account }) => {
        expect(account.isStaff, "the primary account is staff; this test would pass against an admin and prove nothing").toBeFalsy();
        const response = await page.goto(staffRoutes.siteAdmin);
        const status = response?.status() ?? 0;
        const html = await page.content();
        expect(status, "a non-staff account was served site-admin").not.toBe(200);
        expect(html, "a 403/redirect page still rendered the site-admin dashboard").not.toMatch(/site administration|pull latest code|api limits/i);
        expect([403, 404], `site-admin answered ${status}`).toContain(status);
    });

    test("django admin does not let a non-staff session in", async ({ page, account }) => {
        expect(account.isStaff).toBeFalsy();
        const response = await page.goto(staffRoutes.djangoAdmin);
        const html = await page.content();
        const path = new URL(page.url()).pathname;
        expect(html, "django admin's index was served to a non-staff user").not.toMatch(/Site administration/);
        expect(
            path.includes("/admin/login") || (response?.status() ?? 0) >= 400 || html.toLowerCase().includes("log in"),
            `django admin answered ${response?.status()} at ${page.url()}`,
        ).toBeTruthy();
    });

    test("django admin login does not distinguish a real username from a fake one", async ({ browser, account }) => {
        const context = await anonymousContext(browser);
        try {
            const page = await context.newPage();
            await page.goto(staffRoutes.djangoAdminLogin);

            const username = page.locator("#id_username, input[name=username]").first();
            const password = page.locator("#id_password, input[name=password]").first();
            if ((await username.count()) === 0) {
                test.skip(true, "This deployment does not render a django admin login form (admin may be disabled).");
            }

            const errorText = async (): Promise<string> =>
                (await page.locator(".errornote, .errorlist, .errors").allInnerTexts()).join(" | ").replace(/\s+/g, " ").trim();

            const submit = async (user: string): Promise<string> => {
                await username.fill(user);
                await password.fill("definitely-not-the-password");
                await page.locator("form").first().locator("button[type=submit], input[type=submit]").first().click();
                await page.waitForLoadState("domcontentloaded");
                return errorText();
            };

            const existing = await submit(account.username);
            await page.goto(staffRoutes.djangoAdminLogin);
            const unknown = await submit("definitely-not-a-user-91b2c");

            expect(existing, "a failed admin login showed the user nothing").toBeTruthy();
            expect(
                existing,
                "the admin login form gives a different error for a real username than for a fake one, which is an account oracle",
            ).toBe(unknown);
        } finally {
            await context.close();
        }
    });
});

test.describe("health probes stay boring", () => {
    test("readiness is the documented four keys, and none of them is a secret", async ({ request }) => {
        const response = await request.get(publicRoutes.healthReady);
        expect(response.status()).toBe(200);
        const report = (await response.json()) as Record<string, unknown>;
        expect(Object.keys(report).sort()).toEqual(["cache", "db", "migrations", "role"]);
        const blob = JSON.stringify(report);
        expect(blob, "readiness includes a connection string").not.toMatch(/postgres(?:ql)?:\/\//i);
        expect(blob, "readiness includes a password").not.toMatch(/password/i);
        expect(blob).not.toMatch(/SECRET/);
    });
});

test.describe("the API does not advertise internals", () => {
    test("JSON error envelopes do not include traceback or sql", async ({ anonymousApi }) => {
        const response = await anonymousApi.get("whoami/");
        expect(response.status()).toBe(401);
        const body = await response.text();
        await expectNoDebugLeak(body, "unauthenticated whoami");
        expect(body.toLowerCase(), "a 401 included a SQL fragment").not.toMatch(/select .+ from /);
        expect(header(response, "content-type"), "a JSON error was served as HTML").toMatch(/json/i);
    });

    test("the published schema does not document internal rest or admin", async ({ anonymousApi }) => {
        const schema = await anonymousApi.json<{ paths?: Record<string, unknown> }>("get", "schema/", { format: "json" });
        const paths = Object.keys(schema.paths ?? {});
        const leaked = paths.filter(
            (path) => path.includes("/dashboard/rest/") || path.includes("/admin/") || path.includes("/site-admin/"),
        );
        expect(leaked, `the published schema documents internal surfaces:\n  ${leaked.join("\n  ")}`).toHaveLength(0);
    });
});

test.describe("referrers and frames", () => {
    test("HTML pages send a referrer policy that does not leak URLs cross-origin", async ({ page }) => {
        const response = await page.goto(appRoutes.home);
        const policy = (header(response!, "referrer-policy") || "").toLowerCase();
        expect(policy, "no Referrer-Policy was sent").toBeTruthy();
        const allowed = new Set(["same-origin", "no-referrer", "strict-origin", "strict-origin-when-cross-origin"]);
        const tokens = policy.split(/[,\s]+/).filter(Boolean);
        expect(
            tokens.every((token) => allowed.has(token)),
            `Referrer-Policy "${policy}" may send full pin/share URLs to third parties`,
        ).toBeTruthy();
    });
});
