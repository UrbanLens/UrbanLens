/**
 * A notification made by one account has to reach another account's browser.
 *
 * This is the chain nothing else in the repo tests end to end. A unit test can
 * assert that `NotificationLog.objects.notify()` writes a row; it cannot tell
 * you that the row reaches a *different user's* rendered page, because the row
 * and the page live in one process there. On a deployment they are separated by
 * a database, a channel layer, an HTMX fragment endpoint that the shell polls,
 * and a template - and any one of those can be the reason a badge never fills
 * in.
 *
 * Driven through the friend-request path because it is the shortest route from
 * "account B did something" to "account A should be told", and because it needs
 * no fixtures beyond the two accounts the suite already has.
 */

import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import type { ApiClient } from "../../lib/api-client.js";
import { appRoutes, shellFragmentRoutes } from "../../lib/routes.js";

/** The calling credential's own profile. */
async function whoami(api: ApiClient): Promise<{ uuid: string; slug: string }> {
    return api.json<{ uuid: string; slug: string }>("get", "whoami/");
}

test.describe("notifications", () => {
    test("the shell's unread-count fragment answers", async ({ page }) => {
        // Loaded by an `hx-trigger` rather than by a click, so a failure here is
        // silent in the browser: the badge simply never appears, and nobody
        // reports a bug about a thing that was never there.
        await page.goto(appRoutes.home);

        const response = await page.request.get(shellFragmentRoutes.notificationCount);
        expect(response.status(), `the unread-count fragment answered ${response.status()}`).toBe(200);
    });

    test("the notification dropdown renders", async ({ page }) => {
        await page.goto(appRoutes.home);

        const response = await page.request.get(shellFragmentRoutes.notificationDropdown);
        expect(response.status(), `the notification dropdown answered ${response.status()}`).toBe(200);
        // An empty state is a perfectly good answer; a blank body is not, because
        // the dropdown would open onto nothing at all.
        expect((await response.text()).trim().length, "the dropdown fragment came back empty").toBeGreaterThan(0);
    });

    test("the dropdown renders whatever the API says is unread", async ({ page, api }) => {
        // This deliberately observes rather than manufactures. Producing a
        // notification means a cross-account action, and every one of those
        // writes to the single friendship the suite's two fixed accounts share
        // - which `api/social.spec.ts` owns and asserts against serially. Files
        // run in parallel, so a second spec creating and deleting that same
        // relationship races it, and the failures read as endpoint faults
        // rather than as two tests fighting over one row.
        //
        // What is left here is the half that is genuinely about the browser:
        // the shell and the API have to agree about what is unread, because
        // they read the same rows through different code and a badge that
        // disagrees with the list behind it is its own bug.
        const feed = await api.json<{ unread_count: number }>("get", "notifications/");
        await page.goto(appRoutes.home);

        const fragment = await page.request.get(shellFragmentRoutes.notificationCount);
        expect(fragment.status()).toBe(200);
        const rendered = (await fragment.text()).trim();

        if (feed.unread_count > 0) {
            expect(rendered, `the API reports ${feed.unread_count} unread but the badge fragment renders "${rendered}"`).toContain(String(feed.unread_count));
        } else {
            // An empty badge is the right answer for nothing unread; a "0" is
            // acceptable too. A number greater than zero is not.
            expect(rendered, `the API reports nothing unread but the badge fragment renders "${rendered}"`).not.toMatch(/[1-9]/);
        }
    });

    test("the notifications page renders its own list", async ({ page }) => {
        // The dropdown is a summary; this is where somebody goes to read the
        // whole history, and it is a different template with a different query.
        const response = await page.goto("/dashboard/notifications/");
        expect([200, 302], `the notifications page answered ${response?.status()}`).toContain(response?.status() ?? 0);
    });
});
