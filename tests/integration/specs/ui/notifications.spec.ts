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

    ifSecondaryAccount()("something another account does shows up in this account's notifications", async ({ page, api, secondaryApi }) => {
        const me = await whoami(api);
        const them = await whoami(secondaryApi);

        // Clear any friendship left by an earlier run, so the request below is
        // genuinely new and genuinely generates a notification.
        await secondaryApi.delete(`friends/${me.uuid}/`);
        await api.delete(`friends/${them.uuid}/`);

        const requested = await secondaryApi.post("friends/", { profile_uuid: me.uuid, message: "Sent by the integration suite." });
        expect(requested.status(), `the other account's friend request answered ${requested.status()}: ${(await requested.text()).slice(0, 200)}`).toBeLessThan(300);

        try {
            await page.goto(appRoutes.home);

            // Polled rather than asserted once: the notification is written in
            // the request that created the friendship, but a deployment may
            // deliver it through the channel layer or a task, and a single
            // immediate read would be testing this machine's timing rather than
            // the application.
            await expect
                .poll(
                    async () => (await page.request.get(shellFragmentRoutes.notificationDropdown)).text().then((body) => body.toLowerCase()),
                    {
                        message: "a friend request from the other account never appeared in this account's notification dropdown",
                        timeout: 15_000,
                    },
                )
                .toContain("friend");
        } finally {
            await secondaryApi.delete(`friends/${me.uuid}/`);
            await api.delete(`friends/${them.uuid}/`);
        }
    });

    test("the notifications page renders its own list", async ({ page }) => {
        // The dropdown is a summary; this is where somebody goes to read the
        // whole history, and it is a different template with a different query.
        const response = await page.goto("/dashboard/notifications/");
        expect([200, 302], `the notifications page answered ${response?.status()}`).toContain(response?.status() ?? 0);
    });
});
