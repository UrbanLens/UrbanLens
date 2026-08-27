/**
 * The trips pages, driven by data the API made.
 *
 * Setting up over the API and asserting on the rendered page is the pattern the
 * rest of the UI suite uses, and it earns its keep here more than anywhere: a
 * trip's page is a join of trip, activities, pins and locations, so "it renders"
 * is a statement about four tables agreeing, not about one template. Building
 * the trip through the UI instead would spend most of the test proving the
 * create form works and would fail for the wrong reason when it changed.
 */

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import { appRoutes } from "../../lib/routes.js";

interface Trip {
    uuid: string;
    slug: string;
    name: string;
}

/** ISO date `days` from today. */
function isoDate(days: number): string {
    const when = new Date();
    when.setUTCDate(when.getUTCDate() + days);
    return when.toISOString().slice(0, 10);
}

test.describe("trips pages", () => {
    test("a trip created through the API appears on the trips page", async ({ page, api }) => {
        const name = resourceName("visible trip");
        const trip = await api.json<Trip>("post", "trips/", { name, start_date: isoDate(5), end_date: isoDate(6) });
        api.track("trip", trip.slug, () => api.delete(`trips/${trip.slug}/`));

        await page.goto(appRoutes.trips);

        // The list is the surface a user checks after making a trip anywhere
        // else - the mobile client, the map's "add to trip" - so a trip that
        // exists but is not listed is indistinguishable from one that failed.
        await expect(page.locator("body")).toContainText(name);
    });

    test("a trip's own page renders its activities", async ({ page, api }) => {
        const trip = await api.json<Trip>("post", "trips/", { name: resourceName("detailed trip"), start_date: isoDate(2) });
        api.track("trip", trip.slug, () => api.delete(`trips/${trip.slug}/`));
        const pin = await api.createPin({ name: resourceName("trip destination") });

        const title = resourceName("scheduled stop");
        const created = await api.post(`trips/${trip.slug}/activities/`, {
            title,
            pin_slug: pin.slug,
            scheduled_at: `${isoDate(2)}T14:00:00Z`,
        });
        expect(created.status(), `creating the activity answered ${created.status()}: ${(await created.text()).slice(0, 200)}`).toBeLessThan(300);

        const response = await page.goto(`/dashboard/trips/${trip.slug}/`);
        expect(response?.status(), `the trip page answered ${response?.status()}`).toBe(200);
        await expect(page.locator("body")).toContainText(title);
    });

    test("an empty trip renders rather than erroring", async ({ page, api }) => {
        // The zero-activity case is the one a template forgets: the page is
        // built to lay out a schedule and there is nothing to lay out.
        const trip = await api.json<Trip>("post", "trips/", { name: resourceName("empty trip") });
        api.track("trip", trip.slug, () => api.delete(`trips/${trip.slug}/`));

        const response = await page.goto(`/dashboard/trips/${trip.slug}/`);
        expect(response?.status(), "a trip with no activities did not render").toBe(200);
    });

    test("a trip slug that was never issued 404s", async ({ page }) => {
        const response = await page.goto("/dashboard/trips/a-trip-slug-that-never-existed-91b2c/");
        expect(response?.status()).toBe(404);
    });

    test("a deleted trip stops rendering", async ({ page, api }) => {
        const trip = await api.json<Trip>("post", "trips/", { name: resourceName("doomed trip") });
        await page.goto(`/dashboard/trips/${trip.slug}/`);

        await api.delete(`trips/${trip.slug}/`);

        const response = await page.goto(`/dashboard/trips/${trip.slug}/`);
        expect(response?.status(), "a deleted trip's page is still being served").toBe(404);
    });
});
