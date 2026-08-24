/**
 * Visits: the log of when somebody actually went.
 *
 * Small surface, and worth a deployed test for one reason - a visit is the only
 * thing in the API written against a *past* timestamp the client chooses, which
 * makes it the place where timezone handling shows up. A naive round-trip that
 * passes in a UTC test process can shift a visit by hours on a deployment whose
 * database or worker is set to something else.
 */

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";

interface Page<T> {
    count: number;
    results: T[];
}

interface Visit {
    id?: number;
    visit_id?: number;
    uuid?: string;
    notes?: string;
    visited_at?: string;
}

/** The identifier the item routes take, whichever key it arrives under. */
function visitId(visit: Visit): string | number | undefined {
    return visit.id ?? visit.visit_id ?? visit.uuid;
}

test.describe("visits", () => {
    test("a visit can be logged, listed, amended and removed", async ({ api }) => {
        const pin = await api.createPin({ name: resourceName("visited place") });
        const notes = resourceName("visit notes");
        const visitedAt = new Date(Date.now() - 86_400_000).toISOString();

        const created = await api.post(`pins/${pin.slug}/visits/`, { visited_at: visitedAt, notes });
        expect(created.status(), `logging a visit answered ${created.status()}: ${(await created.text()).slice(0, 250)}`).toBeLessThan(300);
        const visit = (await created.json()) as Visit;
        const id = visitId(visit);
        expect(id, `the created visit carries no identifier: ${JSON.stringify(visit).slice(0, 200)}`).toBeTruthy();

        const listed = await api.json<Page<Visit>>("get", `pins/${pin.slug}/visits/`);
        expect(listed.results.some((entry) => visitId(entry) === id), "a visit just logged is missing from the pin's visit list").toBeTruthy();

        const amended = await api.patch(`pins/${pin.slug}/visits/${id}/`, { notes: `${notes} amended` });
        expect(amended.status(), `PATCH answered ${amended.status()}: ${(await amended.text()).slice(0, 200)}`).toBe(200);

        const removed = await api.delete(`pins/${pin.slug}/visits/${id}/`);
        expect(removed.ok(), `DELETE answered ${removed.status()}`).toBeTruthy();
    });

    test("the timestamp a visit is logged with is the timestamp it reads back with", async ({ api }) => {
        const pin = await api.createPin({ name: resourceName("timestamped visit") });
        // A whole number of seconds and unambiguously in the past, so a
        // comparison cannot be confused by sub-second rounding or by a
        // deployment clock that is a little behind this one.
        const visitedAt = new Date(Math.floor((Date.now() - 3 * 86_400_000) / 1000) * 1000).toISOString();

        const created = await api.post(`pins/${pin.slug}/visits/`, { visited_at: visitedAt });
        expect(created.status()).toBeLessThan(300);
        const id = visitId((await created.json()) as Visit);

        const listed = await api.json<Page<Visit>>("get", `pins/${pin.slug}/visits/`);
        const stored = listed.results.find((entry) => visitId(entry) === id);
        expect(stored?.visited_at, "the stored visit has no visited_at").toBeTruthy();

        // Compared as instants rather than as strings: the API is entitled to
        // render the offset differently from the client, and "+00:00" versus
        // "Z" is not a defect. A shift of hours is.
        const sent = Date.parse(visitedAt);
        const back = Date.parse(String(stored?.visited_at));
        expect(
            Math.abs(back - sent),
            `a visit logged at ${visitedAt} read back as ${stored?.visited_at} - a shift of ${Math.round((back - sent) / 60_000)} minutes`,
        ).toBeLessThan(1000);
    });

    test("a visit in the future is refused", async ({ api }) => {
        // A visit is a record of something that happened. Accepting a future
        // one corrupts "last visited" everywhere it is displayed.
        const pin = await api.createPin({ name: resourceName("no time travel") });
        const response = await api.post(`pins/${pin.slug}/visits/`, { visited_at: new Date(Date.now() + 7 * 86_400_000).toISOString() });

        expect([400, 422], `a visit dated a week from now answered ${response.status()}`).toContain(response.status());
    });

    test("a visit cannot be logged against a pin that is not yours", async ({ api }) => {
        const response = await api.post("pins/definitely-not-a-real-pin-slug-91b2c/visits/", { visited_at: new Date().toISOString() });
        expect(response.status()).toBe(404);
        expect(await response.json()).toHaveProperty("error");
    });

    test("a key without the visits scope cannot log one", async ({ api, restrictedApi }) => {
        const pin = await api.createPin({ name: resourceName("scope guarded") });
        const response = await restrictedApi.post(`pins/${pin.slug}/visits/`, { visited_at: new Date(Date.now() - 3600_000).toISOString() });
        expect(response.status(), "a profile:read key logged a visit").toBe(403);
    });
});
