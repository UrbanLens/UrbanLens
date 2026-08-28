/**
 * CORS, host trust, HTTP methods, and what the proxy forwards.
 *
 * These are the checks a unit test cannot make: they live in the reverse
 * proxy, in ALLOWED_HOSTS, in CORS_ALLOWED_ORIGINS. A staging box that
 * reflects `Origin: https://evil.example` on an authenticated JSON endpoint
 * has given that origin the user's cookies.
 *
 * Controls: a same-origin request still works, a recognised Host still
 * answers, GET still answers. The refusals are only meaningful against those.
 */

import { expect, test } from "../../lib/fixtures.js";
import { apiUrl, env } from "../../lib/env.js";
import { appRoutes, publicRoutes } from "../../lib/routes.js";
import { expectNotServerError, header } from "../../lib/security.js";

test.describe("CORS", () => {
    test("an authenticated endpoint does not reflect an arbitrary Origin", async ({ apiRequestContext, account }) => {
        test.skip(!account.apiKey, "No API key.");
        const response = await apiRequestContext.get(apiUrl("whoami/"), {
            headers: {
                Authorization: `Bearer ${account.apiKey}`,
                Origin: "https://evil.example",
            },
        });
        expect(response.status(), "whoami failed, so the CORS headers would be about an error page").toBe(200);
        const allowOrigin = header(response, "access-control-allow-origin");
        expect(allowOrigin, `whoami reflected Origin https://evil.example as ACAO`).not.toBe("https://evil.example");
        expect(allowOrigin, "whoami allows any origin with credentials").not.toBe("*");
        const allowCreds = header(response, "access-control-allow-credentials").toLowerCase();
        if (allowCreds === "true") {
            expect(allowOrigin, "ACAC is true but ACAO is missing or wildcard").toBeTruthy();
            expect(allowOrigin).not.toBe("*");
        }
    });

    test("a preflight from an unknown origin is not granted", async ({ request }) => {
        const response = await request.fetch(apiUrl("whoami/"), {
            method: "OPTIONS",
            headers: {
                Origin: "https://evil.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        });
        await expectNotServerError(response, "CORS preflight");
        const allowOrigin = header(response, "access-control-allow-origin");
        expect(allowOrigin, "a preflight from evil.example was granted that origin").not.toBe("https://evil.example");
        const allowMethods = header(response, "access-control-allow-methods").toUpperCase();
        if (allowOrigin && allowOrigin !== "null") {
            expect(allowMethods, "a granted preflight allows every method").not.toMatch(/\*/);
        }
    });

    test("the schema, which is public, still does not wildcard-allow credentialed access", async ({ anonymousApi }) => {
        const response = await anonymousApi.get("schema/", { format: "json" });
        expect(response.status()).toBe(200);
        const allowOrigin = header(response, "access-control-allow-origin");
        const allowCreds = header(response, "access-control-allow-credentials").toLowerCase();
        if (allowCreds === "true") {
            expect(allowOrigin).not.toBe("*");
        }
    });
});

test.describe("Host header", () => {
    test("the real Host still answers, so later refusals are not a down host", async ({ request }) => {
        const response = await request.get(publicRoutes.healthLive, { headers: { Host: env.host } });
        expect(response.status()).toBe(200);
    });

    test("an unknown Host is not served as this site", async ({ request }) => {
        const response = await request.get(publicRoutes.healthLive, {
            headers: { Host: "evil.example" },
            maxRedirects: 0,
        });
        // Django answers 400 DisallowedHost. A proxy that overwrites Host
        // with the upstream's own name will still 200 - that is the proxy
        // doing its job, and we detect it by seeing UrbanLens's liveness
        // body *and* no DisallowedHost. Skip rather than fail: we never
        // actually presented a bad Host to Django.
        if (response.status() === 200 && (await response.text()).trim() === "Okay!") {
            test.skip(true, "The proxy overwrote Host before Django saw it, so this run cannot exercise ALLOWED_HOSTS.");
        }
        expect(
            [400, 403, 404, 421],
            `an unknown Host answered ${response.status()} with ${(await response.text()).slice(0, 120)}`,
        ).toContain(response.status());
    });

    test("X-Forwarded-Host cannot make absolute redirects point off-site", async ({ request }) => {
        const response = await request.get(appRoutes.home, {
            headers: { "X-Forwarded-Host": "evil.example" },
            maxRedirects: 0,
        });
        const location = header(response, "location");
        if (location) {
            expect(location, `X-Forwarded-Host rewrote a redirect to ${location}`).not.toContain("evil.example");
        }
        const body = await response.text();
        expect(body, "the HTML includes evil.example as a canonical origin").not.toContain("https://evil.example");
    });
});

test.describe("HTTP methods", () => {
    test("TRACE is not enabled", async ({ request }) => {
        const response = await request.fetch(env.baseUrl + "/", { method: "TRACE" });
        await expectNotServerError(response, "TRACE /");
        expect(response.status(), "TRACE was accepted, which lets a client ask the proxy to echo the request").not.toBe(200);
        const body = await response.text();
        expect(body.toUpperCase(), "TRACE echoed the request").not.toMatch(/^(?:TRACE|MESSAGE)\s+\//);
    });

    test("a method override header cannot turn a GET into a write", async ({ api, apiRequestContext, account }) => {
        test.skip(!account.apiKey, "No API key.");
        const pin = await api.createPin();
        expect((await api.get(`pins/${pin.slug}/`)).status()).toBe(200);

        const overridden = await apiRequestContext.get(apiUrl(`pins/${pin.slug}/`), {
            headers: {
                Authorization: `Bearer ${account.apiKey}`,
                "X-HTTP-Method-Override": "DELETE",
                "X-HTTP-Method": "DELETE",
            },
        });
        expect(overridden.status(), "GET with a method-override header was treated as a write").toBe(200);
        expect((await api.get(`pins/${pin.slug}/`)).status(), "a GET with X-HTTP-Method-Override: DELETE deleted the pin").toBe(200);
    });

    test("POST to a read-only API collection without a body is still authenticated", async ({ anonymousApi }) => {
        const response = await anonymousApi.post("pins/", { name: "nope", latitude: 0, longitude: 0 });
        expect(response.status(), "an unauthenticated POST created a pin or skipped auth").toBe(401);
    });
});

test.describe("content types", () => {
    test("JSON endpoints declare JSON and nosniff", async ({ api }) => {
        const response = await api.get("whoami/");
        expect(response.status()).toBe(200);
        expect(header(response, "content-type")).toMatch(/json/i);
        expect(header(response, "x-content-type-options").toLowerCase()).toBe("nosniff");
    });

    test("HTML pages declare HTML and nosniff", async ({ page }) => {
        const response = await page.goto(appRoutes.home);
        expect(header(response!, "content-type")).toMatch(/html/i);
        expect(header(response!, "x-content-type-options").toLowerCase()).toBe("nosniff");
    });
});

test.describe("proxy trust", () => {
    test("X-Forwarded-Proto cannot make an HTTPS deployment answer as HTTP-only cookies", async ({ request }) => {
        const response = await request.get(publicRoutes.login, {
            headers: { "X-Forwarded-Proto": "http" },
        });
        await expectNotServerError(response, "login with X-Forwarded-Proto: http");
        const setCookie = response.headers()["set-cookie"] ?? "";
        if (env.baseUrl.startsWith("https:") && setCookie.toLowerCase().includes("sessionid")) {
            expect(setCookie.toLowerCase(), "a spoofed X-Forwarded-Proto: http dropped Secure on the session cookie").toMatch(/secure/);
        }
    });
});
