/**
 * Surfaces that are easy to grow by accident: the internal REST API, media,
 * webhooks, OAuth, API-key placement, and the export downloader.
 *
 * The published external API is in `specs/api/`. This file is the *other*
 * doors: session-authenticated `/dashboard/rest/`, `/media/`, `/oauth/`,
 * Stripe's CSRF-exempt webhook, and credentials presented somewhere other
 * than `Authorization: Bearer`. Each one has a working control so a 401 is
 * not "the endpoint is gone".
 */

import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import { apiUrl, env, resourceName } from "../../lib/env.js";
import { mediaRoute, oauthRoutes, restRoutes, toolsRoutes } from "../../lib/routes.js";
import {
    anonymousContext,
    expectIndistinguishableFromMissing,
    expectNotServerError,
    expectRefused,
    MISSING_UUID,
    wasRefused,
} from "../../lib/security.js";

test.describe("the internal REST surface is session-only and owner-scoped", () => {
    test("an anonymous PATCH cannot edit a pin", async ({ api, apiRequestContext }) => {
        const pin = await api.createPin({ name: resourceName("rest anonymous") });
        const response = await apiRequestContext.patch(restRoutes.pin(pin.uuid), {
            data: { name: "hijacked anonymously" },
        });
        await expectNotServerError(response, "anonymous PATCH /dashboard/rest/pins/{uuid}/");
        await expectRefused(response, "anonymous PATCH /dashboard/rest/pins/{uuid}/");
        const still = await api.json<{ name: string }>("get", `pins/${pin.slug}/`);
        expect(still.name, "an anonymous REST PATCH still renamed the pin").not.toBe("hijacked anonymously");
    });

    test("a bearer API key cannot use the internal REST surface", async ({ api, apiRequestContext, account }) => {
        test.skip(!account.apiKey, "No API key.");
        const pin = await api.createPin({ name: resourceName("rest bearer") });
        const response = await apiRequestContext.patch(restRoutes.pin(pin.uuid), {
            headers: { Authorization: `Bearer ${account.apiKey}` },
            data: { name: "renamed via leaked rest+key combo" },
        });
        await expectNotServerError(response, "bearer PATCH on /dashboard/rest/");
        // The internal REST is session-authenticated. A bearer key working
        // here would mean the key authenticator was wired into the default
        // REST stack, which authentication.py's module docstring forbids.
        expect(
            wasRefused(response) || response.status() === 403,
            `an API key was accepted by /dashboard/rest/ (${response.status()})`,
        ).toBeTruthy();
        if (response.status() < 300) {
            throw new Error(`bearer PATCH on internal REST succeeded: ${await response.text()}`);
        }
    });

    ifSecondaryAccount()("a signed-in session cannot PATCH another account's pin over REST", async ({ api, secondaryApi, secondaryPage }) => {
        const pin = await api.createPin({ name: resourceName("rest idor") });
        const original = pin.name;

        const response = await secondaryPage.request.patch(restRoutes.pin(pin.uuid), {
            data: { name: "hijacked via rest" },
            headers: { "Content-Type": "application/json" },
        });
        await expectNotServerError(response, "stranger PATCH /dashboard/rest/pins/{uuid}/");
        expect(response.status(), `another account's session renamed a pin over REST (${response.status()})`).toBeGreaterThanOrEqual(400);

        const still = await api.json<{ name: string }>("get", `pins/${pin.slug}/`);
        expect(still.name).toBe(original);

        const missing = await secondaryPage.request.patch(restRoutes.pin(MISSING_UUID), {
            data: { name: "nope" },
            headers: { "Content-Type": "application/json" },
        });
        // CSRF may 403 both; that's still a refusal. When CSRF is satisfied,
        // the two 404s must match.
        if (response.status() !== 403 && missing.status() !== 403) {
            await expectIndistinguishableFromMissing(response, missing, "REST PATCH of another account's pin");
        }
    });

    test("the owner can PATCH their pin over REST with a session, so refusals above are about authz", async ({ api, page }) => {
        const pin = await api.createPin({ name: resourceName("rest control") });
        await page.goto(`/dashboard/map/pin/${pin.slug}/`);
        const csrf = (await page.context().cookies()).find((cookie) => cookie.name === "csrftoken")?.value;
        expect(csrf, "no CSRF cookie, so a REST PATCH from the session would fail for the wrong reason").toBeTruthy();

        const renamed = `${pin.name} rest-edited`;
        const response = await page.request.patch(restRoutes.pin(pin.uuid), {
            data: { name: renamed },
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrf ?? "",
            },
        });
        expect(
            response.status(),
            `the owner could not PATCH their pin over REST (${response.status()}: ${(await response.text()).slice(0, 200)}). If this is 403 CSRF, the control is the one that's broken.`,
        ).toBe(200);
        const after = await api.json<{ name: string }>("get", `pins/${pin.slug}/`);
        expect(after.name).toBe(renamed);
    });
});

test.describe("API keys only work as Bearer tokens", () => {
    test("a key in the query string does not authenticate", async ({ apiRequestContext, account }) => {
        const key = account.apiKey;
        test.skip(!key, "No API key.");
        if (!key) {
            return;
        }
        const response = await apiRequestContext.get(`${apiUrl("whoami/")}?api_key=${encodeURIComponent(key)}`);
        expect(response.status(), "an API key in the query string authenticated, which puts it in logs and Referer").toBe(401);
    });

    test("a key in a cookie does not authenticate", async ({ apiRequestContext, account }) => {
        test.skip(!account.apiKey, "No API key.");
        const response = await apiRequestContext.get(apiUrl("whoami/"), {
            headers: { Cookie: `api_key=${account.apiKey}; sessionid=${account.apiKey}` },
        });
        expect(response.status(), "an API key in a cookie authenticated").toBe(401);
    });

    test("HTTP Basic with the account password does not authenticate to the external API", async ({ apiRequestContext, account }) => {
        const token = Buffer.from(`${account.username}:${account.password}`).toString("base64");
        const response = await apiRequestContext.get(apiUrl("whoami/"), {
            headers: { Authorization: `Basic ${token}` },
        });
        expect(response.status(), "HTTP Basic with the user's password was accepted by the external API").toBe(401);
    });

    test("the same key as Bearer still works, so the refusals above are about placement", async ({ api }) => {
        expect((await api.get("whoami/")).status()).toBe(200);
    });
});

test.describe("media is gated", () => {
    test("an anonymous request for a pin image path is refused", async ({ browser }) => {
        const context = await anonymousContext(browser);
        try {
            const response = await context.request.get(mediaRoute("pin_images/does-not-exist-91b2c.png"), { maxRedirects: 0 });
            await expectNotServerError(response, "anonymous /media/pin_images/...");
            await expectRefused(response, "anonymous /media/pin_images/...");
        } finally {
            await context.close();
        }
    });

    ifSecondaryAccount()("another account cannot fetch this account's photo bytes by URL", async ({ api, apiRequestContext, account, secondaryApi }) => {
        test.skip(!account.apiKey, "No API key.");
        test.skip(!secondaryApi.apiKey, "Secondary has no API key.");
        const pin = await api.createPin({ name: resourceName("media gate") });
        const png = Buffer.concat([
            Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64"),
            Buffer.from(`\n${resourceName("media")}`, "utf-8"),
        ]);
        const upload = await apiRequestContext.post(apiUrl("photos/"), {
            headers: { Authorization: `Bearer ${account.apiKey}` },
            multipart: {
                file: { name: "sec-media.png", mimeType: "image/png", buffer: png },
                caption: resourceName("media caption"),
                pin: pin.slug,
            },
        });
        test.skip(upload.status() === 503, `malware scanner unavailable: ${(await upload.text()).slice(0, 120)}`);
        expect(upload.status()).toBeLessThan(300);
        const photo = (await upload.json()) as { uuid: string; url?: string };
        api.track("photo", photo.uuid, () => api.delete(`photos/${photo.uuid}/`));
        expect(photo.url, "upload returned no url").toBeTruthy();

        const target = (photo.url ?? "").startsWith("http") ? photo.url! : new URL(photo.url ?? "", env.baseUrl).toString();
        if (!target.startsWith(env.baseUrl)) {
            test.skip(true, `Photo URL is off-origin (${target}); object-store signatures are a different contract.`);
        }

        const owner = await apiRequestContext.get(target, { headers: { Authorization: `Bearer ${account.apiKey}` } });
        expect(owner.status(), "the owner cannot fetch their own photo URL, so the stranger's refusal would prove nothing").toBe(200);

        const stranger = await apiRequestContext.get(target, {
            headers: { Authorization: `Bearer ${secondaryApi.apiKey}` },
        });
        await expectNotServerError(stranger, "stranger GET of a private photo URL");
        expect(stranger.status(), `another account fetched this photo's bytes (${stranger.status()})`).toBeGreaterThanOrEqual(400);
    });
});

test.describe("export jobs are not world-readable", () => {
    test("a random export id is not downloadable", async ({ page }) => {
        await page.goto("/dashboard/tools/");
        const response = await page.request.get(toolsRoutes.exportDownload(MISSING_UUID), { maxRedirects: 0 });
        await expectNotServerError(response, "export download of a random uuid");
        expect(headerContentType(response), "a random export uuid streamed a zip").not.toMatch(/zip|octet-stream/i);
        expect(response.status(), `export download of a random uuid answered ${response.status()}`).not.toBe(200);
    });

    ifSecondaryAccount()("export status of a random job looks the same to both accounts", async ({ page, secondaryPage }) => {
        await page.goto("/dashboard/tools/");
        await secondaryPage.goto("/dashboard/tools/");
        const jobId = MISSING_UUID;
        const mine = await page.request.get(toolsRoutes.exportStatus(jobId));
        const theirs = await secondaryPage.request.get(toolsRoutes.exportStatus(jobId));
        await expectNotServerError(mine, "export status (primary)");
        await expectNotServerError(theirs, "export status (secondary)");
        // Both should be error fragments, not a progress bar of somebody's job.
        for (const body of [await mine.text(), await theirs.text()]) {
            expect(body.toLowerCase(), "export status of a missing job included a download link").not.toMatch(/download export|export\.zip/);
        }
    });
});

test.describe("OAuth and webhooks", () => {
    test("token introspection without a credential is refused", async ({ request }) => {
        const response = await request.post(oauthRoutes.introspect, {
            form: { token: "not-a-token" },
            maxRedirects: 0,
        });
        await expectNotServerError(response, "oauth introspect");
        expect(wasRefused(response) || response.status() === 400 || response.status() === 405, `oauth introspect answered ${response.status()}`).toBeTruthy();
        if (response.status() === 200) {
            const body = await response.text();
            expect(body, "introspection of a fake token returned an active token").not.toMatch(/"active"\s*:\s*true/);
        }
    });

    test("the Stripe webhook does not accept an unsigned POST", async ({ request }) => {
        const response = await request.post("/dashboard/billing/webhooks/stripe/", {
            data: { type: "ping" },
            headers: { "Content-Type": "application/json" },
        });
        await expectNotServerError(response, "unsigned stripe webhook");
        expect(
            [400, 401, 403, 404, 405],
            `an unsigned Stripe webhook answered ${response.status()}, which would let anyone forge billing events`,
        ).toContain(response.status());
    });
});

test.describe("password reset is not an account oracle", () => {
    test("resetting a real address and a fake one looks the same", async ({ browser, account }) => {
        test.skip(!account.email, "The primary account has no email in the manifest.");
        const context = await anonymousContext(browser);
        try {
            const page = await context.newPage();
            await page.goto("/accounts/password_reset/");
            const email = page.locator("#id_email, input[name=email]").first();
            if ((await email.count()) === 0) {
                test.skip(true, "No password-reset email field on this deployment.");
            }

            const submit = async (address: string): Promise<{ text: string; url: string }> => {
                await page.goto("/accounts/password_reset/");
                const field = page.locator("#id_email, input[name=email]").first();
                await field.fill(address);
                await page.locator("form").first().locator("button[type=submit], input[type=submit]").first().click();
                await page.waitForLoadState("domcontentloaded");
                return { text: (await page.locator("body").innerText()).replace(/\s+/g, " ").trim(), url: new URL(page.url()).pathname };
            };

            const existing = await submit(account.email);
            const unknown = await submit("nobody-91b2c@e2e.invalid");
            expect(existing.url, "a real address was sent somewhere other than the 'email sent' page").toBe(unknown.url);
            const redact = (text: string) => text.replace(/[^\s]+@[^\s]+/g, "[email]");
            expect(
                redact(existing.text),
                "password reset gives a different page for a real address than for a fake one, which is an account oracle",
            ).toBe(redact(unknown.text));
        } finally {
            await context.close();
        }
    });
});

function headerContentType(response: { headers: () => Record<string, string> }): string {
    return response.headers()["content-type"] ?? "";
}
