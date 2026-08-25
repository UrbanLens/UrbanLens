/**
 * The application's collection pages, rendering real rows.
 *
 * `smoke/pages.spec.ts` visits every page and checks it does not error. That is
 * the right first question and it has a blind spot: a page that renders its
 * empty state perfectly answers 200 whether or not it can display anything. All
 * of these pages are lists, so the interesting failure - a template that breaks
 * on a row, an N+1 that only appears with data, a serializer that assumes a
 * field a real row leaves null - needs a row to exist.
 *
 * So each test here creates something through the API and then asks the page
 * about it. The console guard is doing as much work as the assertions: it fails
 * the test if the page throws or 404s a subresource while rendering, which is
 * how the fan-out problems on the pin detail page surfaced.
 */

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import { appRoutes } from "../../lib/routes.js";

interface Named {
    uuid?: string;
    slug?: string;
    name?: string;
}

test.describe("collection pages", () => {
    test("the lists page shows a list that exists", async ({ page, api }) => {
        const name = resourceName("visible list");
        const list = await api.json<Named>("post", "lists/", { name });
        api.track("list", String(list.slug), () => api.delete(`lists/${list.slug}/`));

        await page.goto(appRoutes.lists);

        await expect(page.locator("body")).toContainText(name);
    });

    test("the organize page renders with pins present", async ({ page, api }) => {
        // Organize is the bulk-editing surface, so it renders every pin with
        // controls attached - the page most likely to gain a per-row query or a
        // template that assumes a field.
        await api.createPin({ name: resourceName("organizable pin") });

        const response = await page.goto(appRoutes.organize);
        expect(response?.status(), `the organize page answered ${response?.status()}`).toBe(200);
    });

    test("the memories page renders", async ({ page }) => {
        const response = await page.goto(appRoutes.memories);
        expect(response?.status(), `the memories page answered ${response?.status()}`).toBe(200);
    });

    test("the settings page renders its sections", async ({ page }) => {
        const response = await page.goto(appRoutes.settings);
        expect(response?.status()).toBe(200);
        // A settings page that renders a shell with no controls is a template
        // that failed quietly, and it answers 200 either way.
        await expect(page.locator("form, input, select").first()).toBeVisible();
    });

    test("the profile page renders", async ({ page }) => {
        const response = await page.goto(appRoutes.profile);
        expect(response?.status()).toBe(200);
    });

    test("the achievements page renders", async ({ page }) => {
        const response = await page.goto(appRoutes.achievements);
        expect(response?.status()).toBe(200);
    });

    test("the safety page renders with a check-in open", async ({ page, api }) => {
        // The open-check-in state is the one the page is really for, and it is
        // not the state a freshly provisioned account is in.
        const created = await api.post("safety/checkins/", {
            title: resourceName("page check-in"),
            checkin_by: new Date(Date.now() + 3600_000).toISOString(),
        });

        try {
            const response = await page.goto(appRoutes.safety);
            expect(response?.status(), `the safety page answered ${response?.status()}`).toBe(200);
        } finally {
            if (created.ok()) {
                const checkin = (await created.json()) as Named;
                await api.post(`safety/checkins/${checkin.slug}/cancel/`);
                await api.delete(`safety/checkins/${checkin.slug}/`);
            }
        }
    });

    test("the trips page shows a trip that exists", async ({ page, api }) => {
        const name = resourceName("page trip");
        const trip = await api.json<Named>("post", "trips/", { name });
        api.track("trip", String(trip.slug), () => api.delete(`trips/${trip.slug}/`));

        await page.goto(appRoutes.trips);

        await expect(page.locator("body")).toContainText(name);
    });

    test("the tools page renders", async ({ page }) => {
        const response = await page.goto(appRoutes.tools);
        expect(response?.status()).toBe(200);
    });

    test("every shell fragment answers on a signed-in page", async ({ page }) => {
        // These load by `hx-trigger` on every page, so a failure is silent in
        // the browser: the badge never fills in and nobody files a bug about a
        // thing that was never there.
        await page.goto(appRoutes.home);

        const fragments = [
            "/dashboard/notifications/unread-count/",
            "/dashboard/notifications/dropdown/",
            "/dashboard/messages/unread-count/",
            "/dashboard/messages/dropdown/",
            "/dashboard/safety/nav-banner/",
        ];

        const failures: string[] = [];
        for (const fragment of fragments) {
            const response = await page.request.get(fragment);
            if (response.status() !== 200) {
                failures.push(`${fragment} -> ${response.status()}`);
            }
        }

        expect(failures, `shell fragments the every-page chrome depends on:\n  ${failures.join("\n  ")}`).toHaveLength(0);
    });
});
