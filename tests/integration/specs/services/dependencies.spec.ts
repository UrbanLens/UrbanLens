/**
 * The stateful dependencies, exercised through the application rather than probed.
 *
 * `/health/ready` answers "the database replied to `SELECT 1`" and "the cache
 * round-tripped a key". Both are worth knowing and neither is what the
 * application needs: it needs to *write* to a database that may be a replica,
 * and it needs a session to still exist on the next request, which is the cache
 * doing a job a ping does not exercise.
 *
 * These also prove the processes agree. A row written through the API and read
 * back through the web UI has crossed from gunicorn to gunicorn via Postgres; a
 * session minted in one worker and honoured by another has crossed via the
 * session store. On a multi-container deployment those are the failures that
 * only appear under real traffic.
 */

import { expect, test } from "../../lib/fixtures.js";
import { env } from "../../lib/env.js";
import { appRoutes, pinDetail } from "../../lib/routes.js";
import { MapPage } from "../../lib/pages/map-page.js";

test.describe("stateful dependencies", () => {
    test("a row written through the API is visible through the web UI", async ({ page, api }) => {
        const pin = await api.createPin();

        // Two entry points, two processes, one database. A read-only replica or
        // a split-brain configuration fails here and nowhere else.
        const response = await page.goto(pinDetail(pin.slug));
        expect(response?.status(), `the pin created over the API 404s in the UI (${pinDetail(pin.slug)})`).toBe(200);
        await expect(page.locator("body")).toContainText(pin.name);
    });

    test("a row deleted through the API stops being served", async ({ page, api }) => {
        const pin = await api.createPin();
        const deleted = await api.delete(`pins/${pin.slug}/`);
        expect(deleted.ok(), `deleting the pin answered ${deleted.status()}`).toBeTruthy();

        const response = await page.goto(pinDetail(pin.slug));
        // A cached page served after the row is gone is a genuine defect, and
        // one that only shows up on a deployment with a cache in front.
        expect(response?.status(), "a deleted pin is still being served").toBe(404);
    });

    test("the map's own pin feed reflects what the API wrote", async ({ page, api }) => {
        const pin = await api.createPin();
        const map = new MapPage(page);
        await map.goto();

        const feed = await map.fetchPins();
        expect(feed.length, "the map's pin feed came back empty for an account that has pins").toBeGreaterThan(0);
        expect(
            feed.some((entry) => entry.slug === pin.slug || entry.uuid === pin.uuid),
            "a pin created over the API is missing from the map's own feed",
        ).toBeTruthy();
    });

    test("a session survives across separate requests", async ({ page }) => {
        // The session store is the cache. A cache that answers a health ping
        // but drops writes signs everyone out at random, which is close to
        // undiagnosable from the outside.
        await page.goto(appRoutes.home);
        await expect(page.locator("#nav-user-btn")).toBeVisible();

        await page.goto(appRoutes.settings);
        expect(new URL(page.url()).pathname, "the session was lost between two requests").not.toContain("/accounts/login");
        await expect(page.locator("#nav-user-btn")).toBeVisible();
    });

    test("REData is reachable from here", async ({ request }) => {
        test.skip(env.redataUrl === null, "Set UL_E2E_REDATA_URL to check the companion service.");

        // A reachability check, not a contract test: REData is a separate
        // application with its own suite. What matters here is that this
        // deployment's dependency is answering at all, since a pin detail page
        // quietly loses its parcel, permit and imagery panels when it is not.
        const response = await request.get(env.redataUrl!, { failOnStatusCode: false });
        expect(response.status(), `REData at ${env.redataUrl} answered ${response.status()}`).toBeLessThan(500);
    });
});
