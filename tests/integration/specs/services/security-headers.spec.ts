/**
 * The response headers a deployment is responsible for.
 *
 * These are settings, not code, and settings differ per environment - which is
 * exactly why a unit test cannot check them and a run against the real
 * deployment can. Every one of them has been silently lost by a proxy rewrite
 * or an environment variable at some point in some project; none of them is
 * visible in a browser unless somebody opens devtools and looks.
 *
 * Assertions are about presence and shape rather than exact values, because the
 * values are legitimately per-environment. A staging instance served over plain
 * HTTP will not carry HSTS, and should not.
 */

import { expect, test } from "../../lib/fixtures.js";
import { env } from "../../lib/env.js";
import { appRoutes, publicRoutes } from "../../lib/routes.js";

const isHttps = env.baseUrl.startsWith("https:");

test.describe("response headers", () => {
    test("clickjacking and MIME sniffing are refused", async ({ request }) => {
        const response = await request.get(appRoutes.home);
        const headers = response.headers();

        expect(headers["x-frame-options"]?.toUpperCase(), "the site can be framed").toBe("DENY");
        expect(headers["x-content-type-options"], "a browser may sniff a response's type").toBe("nosniff");
        expect(headers["referrer-policy"], "no referrer policy is set").toBeTruthy();
    });

    test("a Content-Security-Policy is emitted", async ({ request }) => {
        const response = await request.get(appRoutes.home);
        const headers = response.headers();

        const enforced = headers["content-security-policy"];
        const reportOnly = headers["content-security-policy-report-only"];
        expect(enforced ?? reportOnly, "neither a CSP nor a report-only CSP was sent").toBeTruthy();

        // Report-only is the documented default until an environment's reports
        // are clean, so it is reported rather than failed - but it is worth
        // saying out loud, because a report-only policy blocks nothing.
        if (!enforced && reportOnly) {
            test.info().annotations.push({ type: "note", description: "CSP is report-only on this deployment; it is not enforcing." });
        }
    });

    test("HTTPS is asserted to the browser", async ({ request }) => {
        test.skip(!isHttps, "This deployment is served over plain HTTP, so HSTS would be meaningless.");

        const response = await request.get(appRoutes.home);
        const hsts = response.headers()["strict-transport-security"];
        expect(hsts, "no Strict-Transport-Security header on an HTTPS deployment").toBeTruthy();
        expect(hsts, `HSTS max-age is too short to be useful: "${hsts}"`).toMatch(/max-age=\d{5,}/);
    });

    test("health probes stay exempt from the HTTPS redirect", async ({ request }) => {
        // `SECURE_REDIRECT_EXEMPT` carries `^health` on purpose: a container
        // probe hitting http://localhost:8000/health/ must get 200, not a 301
        // to a hostname it cannot resolve. Losing that exemption marks the
        // container unhealthy and blocks Daphne from starting.
        const response = await request.get(publicRoutes.healthLive, { maxRedirects: 0 });
        expect(response.status(), "the health probe answered a redirect rather than a status").toBe(200);
    });

    test("session and CSRF cookies are protected", async ({ page }) => {
        await page.goto(appRoutes.home);
        const cookies = await page.context().cookies();

        const session = cookies.find((cookie) => cookie.name === "sessionid");
        expect(session, "no session cookie was set for a signed-in page").toBeTruthy();
        expect(session?.httpOnly, "the session cookie is readable by JavaScript").toBeTruthy();
        if (isHttps) {
            expect(session?.secure, "the session cookie is not marked Secure on an HTTPS deployment").toBeTruthy();
        }

        const csrf = cookies.find((cookie) => cookie.name === "csrftoken");
        expect(csrf, "no CSRF cookie was set").toBeTruthy();
        if (isHttps) {
            expect(csrf?.secure, "the CSRF cookie is not marked Secure on an HTTPS deployment").toBeTruthy();
        }
    });

    test("the server does not advertise what it is running", async ({ request }) => {
        const response = await request.get(appRoutes.home);
        const server = response.headers()["server"] ?? "";
        // Not a vulnerability by itself, but a precise version string is free
        // reconnaissance: it tells an attacker exactly which published
        // advisories to try. One line at the proxy removes it.
        expect(server, `the Server header names a version: "${server}". Set "server_tokens off;" in the nginx config (or the equivalent on whatever terminates TLS).`).not.toMatch(/\d+\.\d+\.\d+/);
    });
});
