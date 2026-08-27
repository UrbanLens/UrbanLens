/**
 * The third-party origins the application cannot start without.
 *
 * `themes/base.html` loads jQuery, toastr and HTMX from public CDNs, and the
 * pin detail page adds Leaflet and Leaflet.draw. These are not enhancements:
 * every HTMX interaction, every toast, and every map on the site is dead
 * without them. That makes an outage at one of those hosts - or a
 * Content-Security-Policy change that stops permitting them, or a network that
 * cannot reach them - an outage of this application, produced entirely outside
 * it.
 *
 * The origins are discovered from the rendered page rather than listed here, so
 * a dependency added or removed later is covered either way.
 */

import { expect, test } from "../../lib/fixtures.js";
import { appRoutes } from "../../lib/routes.js";

/** One cross-origin asset a document references. */
interface ExternalAsset {
    url: string;
    integrity: string;
    kind: "script" | "stylesheet";
}

/** Cross-origin scripts and stylesheets a document references. */
async function externalAssets(page: import("@playwright/test").Page): Promise<ExternalAsset[]> {
    return page.evaluate(() => {
        const found: Array<{ url: string; integrity: string; kind: "script" | "stylesheet" }> = [];
        const consider = (url: string, integrity: string | null, kind: "script" | "stylesheet"): void => {
            if (!url) {
                return;
            }
            try {
                if (new URL(url).origin !== window.location.origin) {
                    found.push({ url, integrity: integrity ?? "", kind });
                }
            } catch {
                // A relative or data: URL. Not cross-origin, nothing to check.
            }
        };
        for (const script of document.querySelectorAll<HTMLScriptElement>("script[src]")) {
            consider(script.src, script.getAttribute("integrity"), "script");
        }
        for (const link of document.querySelectorAll<HTMLLinkElement>('link[rel="stylesheet"][href]')) {
            consider(link.href, link.getAttribute("integrity"), "stylesheet");
        }
        return found;
    });
}

test.describe("third-party dependencies", () => {
    test("the libraries the page shell depends on actually loaded", async ({ page }) => {
        await page.goto(appRoutes.home);

        // Checked as globals rather than as network responses: a script that
        // was fetched but failed to evaluate leaves a 200 in the network log
        // and a broken page, and only the global tells the two apart.
        const present = await page.evaluate(() => ({
            jquery: typeof (window as unknown as { jQuery?: unknown }).jQuery !== "undefined",
            toastr: typeof (window as unknown as { toastr?: unknown }).toastr !== "undefined",
            htmx: typeof (window as unknown as { htmx?: unknown }).htmx !== "undefined",
        }));

        const missing = Object.entries(present)
            .filter(([, ok]) => !ok)
            .map(([name]) => name);
        expect(missing, `these libraries did not load: ${missing.join(", ")}. Every HTMX interaction and every toast on the site depends on them.`).toHaveLength(0);
    });

    test("every external asset the shell references is reachable", async ({ page }) => {
        await page.goto(appRoutes.home);
        const assets = await externalAssets(page);
        test.skip(assets.length === 0, "This deployment self-hosts its front-end libraries, so there is no third-party origin to check.");

        const unreachable: string[] = [];
        for (const asset of assets) {
            const response = await page.request.get(asset.url).catch(() => null);
            if (response === null || !response.ok()) {
                unreachable.push(`${response?.status() ?? "network error"} ${asset.url}`);
            }
        }
        expect(unreachable, `third-party assets this application needs are unreachable:\n  ${unreachable.join("\n  ")}`).toHaveLength(0);
    });

    test("external scripts are pinned with subresource integrity", async ({ page }) => {
        await page.goto(appRoutes.home);
        const assets = await externalAssets(page);
        test.skip(assets.length === 0, "No third-party assets on this page.");

        // Scripts only. A compromised stylesheet is a real but much narrower
        // problem than a compromised script, which is arbitrary code with the
        // session's full authority - and font providers legitimately cannot
        // carry a hash at all: Google Fonts serves a different stylesheet per
        // user agent, so no static integrity value can ever match it.
        const executable = assets.filter((asset) => asset.kind === "script");
        const unpinned = executable.filter((asset) => !asset.integrity).map((asset) => asset.url);

        expect(
            unpinned,
            `third-party scripts loaded without an integrity attribute:\n  ${unpinned.join("\n  ")}\n` +
                "Without SRI, whoever controls that CDN controls this application. Add integrity= and crossorigin= alongside the src.",
        ).toHaveLength(0);
    });

    test("the pin detail page's mapping libraries load", async ({ page, api }) => {
        const pin = await api.createPin();
        await page.goto(`/dashboard/map/pin/${pin.slug}/`);

        const hasLeaflet = await page.evaluate(() => typeof (window as unknown as { L?: unknown }).L !== "undefined");
        expect(hasLeaflet, "Leaflet did not load, so every map on this page is a grey rectangle").toBeTruthy();
    });
});
