/**
 * Trips, and the activities hanging off them.
 *
 * The largest domain the suite did not touch. Its shape is what makes it worth
 * a deployed test: a trip owns activities, activities can point at a pin, and
 * the trip's map endpoint reads back across both. Each join is a place where a
 * missing `select_related` or a serializer that assumes a pin is present turns
 * into a 500 for one particular arrangement of data - and the arrangement is
 * the part a fixture picks for you.
 */

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";

interface Page<T> {
    count: number;
    results: T[];
}

interface Trip {
    uuid: string;
    slug: string;
    name: string;
}

/** ISO date `days` from today, for scheduling. */
function isoDate(days: number): string {
    const when = new Date();
    when.setUTCDate(when.getUTCDate() + days);
    return when.toISOString().slice(0, 10);
}

test.describe("trips", () => {
    test("a trip can be created, listed, edited and deleted", async ({ api }) => {
        const name = resourceName("trip lifecycle");
        const created = await api.json<Trip>("post", "trips/", {
            name,
            description: "Created by the UrbanLens integration suite.",
            start_date: isoDate(7),
            end_date: isoDate(9),
        });
        expect(created.slug, `the created trip carries no slug: ${JSON.stringify(created)}`).toBeTruthy();
        api.track("trip", created.slug, () => api.delete(`trips/${created.slug}/`));

        const page = await api.json<Page<Trip>>("get", "trips/", { page_size: "100" });
        expect(page.results.some((trip) => trip.slug === created.slug), "a trip just created is missing from the list").toBeTruthy();

        const renamed = `${name} edited`;
        const patched = await api.patch(`trips/${created.slug}/`, { name: renamed });
        expect(patched.status(), `PATCH answered ${patched.status()}: ${await patched.text()}`).toBe(200);
        expect((await api.json<Trip>("get", `trips/${created.slug}/`)).name).toBe(renamed);

        const removed = await api.delete(`trips/${created.slug}/`);
        expect(removed.ok(), `DELETE answered ${removed.status()}`).toBeTruthy();
        expect((await api.get(`trips/${created.slug}/`)).status()).toBe(404);
    });

    test("an activity pointing at a pin round-trips, and reaches the trip's map", async ({ api }) => {
        const trip = await api.json<Trip>("post", "trips/", { name: resourceName("trip with a stop"), start_date: isoDate(3) });
        api.track("trip", trip.slug, () => api.delete(`trips/${trip.slug}/`));
        const pin = await api.createPin({ name: resourceName("trip stop") });

        const title = resourceName("visit the stop");
        const activity = await api.post(`trips/${trip.slug}/activities/`, {
            title,
            pin_slug: pin.slug,
            scheduled_at: `${isoDate(3)}T15:00:00Z`,
            notes: "Added by the integration suite.",
        });
        expect(activity.status(), `creating an activity answered ${activity.status()}: ${await activity.text()}`).toBeLessThan(300);

        const listed = await api.get(`trips/${trip.slug}/activities/`);
        expect(listed.status()).toBe(200);
        expect(await listed.text(), "the activity just created is not in the trip's activity list").toContain(title);

        // The map endpoint is the one that has to join a trip to its
        // activities to their pins to their locations; a pin whose coordinates
        // live on the Location rather than the Pin is exactly where that join
        // has gone wrong before.
        const map = await api.get(`trips/${trip.slug}/map/`);
        expect(map.status(), `the trip map answered ${map.status()}: ${(await map.text()).slice(0, 200)}`).toBe(200);
    });

    test("an activity cannot be attached to somebody else's trip slug", async ({ api }) => {
        const response = await api.post("trips/definitely-not-a-real-trip-slug-4c1a/activities/", { title: resourceName("orphan") });
        expect(response.status()).toBe(404);
        expect(await response.json()).toHaveProperty("error");
    });

    test("the trip list pages with the documented envelope", async ({ api }) => {
        const response = await api.get("trips/", { page_size: 5 });
        expect(response.status()).toBe(200);
        const body = (await response.json()) as Record<string, unknown>;
        for (const key of ["count", "next", "previous", "results"]) {
            expect(body, `the trips paging envelope is missing "${key}"`).toHaveProperty(key);
        }
    });

    test("a key without the trips scope cannot create one", async ({ restrictedApi }) => {
        const response = await restrictedApi.post("trips/", { name: resourceName("forbidden trip") });
        expect(response.status(), "a profile:read key created a trip").toBe(403);
    });
});
