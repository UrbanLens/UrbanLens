/**
 * The static pipeline, which is invisible until it is completely broken.
 *
 * Deployed instances serve static files through
 * `CompressedManifestStaticFilesStorage`, which hashes every filename and
 * resolves `{% static %}` through a manifest built by `collectstatic`. Two
 * things follow, and both are failure modes a template test cannot see:
 *
 * - An image whose `collectstatic` did not run, or ran against a stale tree,
 *   serves a page whose scripts 404. The HTML is correct; the application is
 *   inert.
 * - A hashed URL is content-addressed and therefore safe to cache forever. If
 *   it is being served with a short or absent cache lifetime, every visitor
 *   re-downloads the whole bundle on every page.
 *
 * Assertions here are about *what the page actually asked for*, discovered from
 * the rendered document, rather than a hardcoded list of asset names that would
 * go stale the first time a bundle was renamed.
 */

import { expect, test } from "../../lib/fixtures.js";
import { appRoutes } from "../../lib/routes.js";

/** Same-origin scripts and stylesheets the document references. */
async function localAssets(page: import("@playwright/test").Page): Promise<string[]> {
    return page.evaluate(() => {
        const urls = new Set<string>();
        for (const script of document.querySelectorAll<HTMLScriptElement>("script[src]")) {
            urls.add(script.src);
        }
        for (const link of document.querySelectorAll<HTMLLinkElement>('link[rel="stylesheet"][href]')) {
            urls.add(link.href);
        }
        return [...urls].filter((url) => new URL(url).origin === window.location.origin);
    });
}

test.describe("static assets", () => {
    test("every script and stylesheet the map page loads actually resolves", async ({ page }) => {
        await page.goto(appRoutes.map);
        const assets = await localAssets(page);

        expect(assets.length, "the page referenced no local scripts or stylesheets at all").toBeGreaterThan(0);

        const broken: string[] = [];
        for (const url of assets) {
            const response = await page.request.get(url);
            if (!response.ok()) {
                broken.push(`${response.status()} ${url}`);
            }
        }
        expect(broken, `assets the page asked for and did not get:\n  ${broken.join("\n  ")}`).toHaveLength(0);
    });

    test("assets are served with a content type a browser will honour", async ({ page }) => {
        await page.goto(appRoutes.map);
        const assets = await localAssets(page);

        const wrong: string[] = [];
        for (const url of assets) {
            const response = await page.request.get(url);
            const contentType = (response.headers()["content-type"] ?? "").toLowerCase();
            const expected = url.includes(".css") ? "css" : "javascript";
            // A proxy misconfiguration that serves JavaScript as text/plain is
            // fatal for a module script and produces no HTTP error at all.
            if (!contentType.includes(expected)) {
                wrong.push(`${url} -> ${contentType || "(none)"}`);
            }
        }
        expect(wrong, `assets served with the wrong content type:\n  ${wrong.join("\n  ")}`).toHaveLength(0);
    });

    test("hashed asset URLs are cacheable", async ({ page }) => {
        await page.goto(appRoutes.map);
        const assets = await localAssets(page);

        // A manifest-hashed name looks like `core.4f3a91c2d0b1.js`: content
        // addressed, so its content can never change under the same URL.
        const hashed = assets.filter((url) => /\.[0-9a-f]{8,}\.(js|css)$/.test(new URL(url).pathname));
        test.skip(hashed.length === 0, "This deployment serves unhashed static files, so there is no cache lifetime to assert.");

        const first = hashed[0]!;
        const response = await page.request.get(first);
        const cacheControl = response.headers()["cache-control"] ?? "";
        expect(cacheControl, `${first} was served with Cache-Control: "${cacheControl}"`).toMatch(/max-age=\d{4,}/);
    });

    test("a hashed asset name that does not exist is refused rather than guessed at", async ({ page }) => {
        const response = await page.request.get("/static/dashboard/js/does-not-exist.0000000000.js");
        // A 200 here means something upstream is serving a fallback body for
        // any path, which turns every missing asset into a silent syntax error.
        expect(response.status()).toBeGreaterThanOrEqual(400);
    });

    test("media is gated rather than served to anyone who guesses a path", async ({ page }) => {
        const response = await page.request.get("/media/definitely-not-a-real-file.jpg", { maxRedirects: 0 });
        // Every /media/ request goes through MediaGateView, which authenticates
        // and authorises. Anything but a refusal means uploads are public.
        expect([301, 302, 403, 404]).toContain(response.status());
    });
});
