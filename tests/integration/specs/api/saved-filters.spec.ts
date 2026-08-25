/**
 * Saved filters: a stored blob of criteria that other features build on.
 *
 * Worth its own file because `criteria` is free-form JSON the API stores and
 * hands back, and two different things then consume it - the map's filter UI
 * and smart lists, which recompute their membership from it. A round trip that
 * loses a key, coerces a number to a string, or drops a nested object is
 * invisible at the point of storage and shows up later as a list that quietly
 * contains the wrong pins.
 *
 * So the assertion is a *deep* comparison of what came back against what went
 * in, rather than "a filter was created".
 */

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";

interface Page<T> {
    count: number;
    results: T[];
}

interface SavedFilter {
    uuid: string;
    name: string;
    criteria?: unknown;
    color?: string | null;
    icon?: string | null;
    order?: number;
}

/**
 * A criteria object with more than one shape in it.
 *
 * A flat string map would round-trip through almost any mistake; nesting, a
 * list, a number and a boolean are what catch a serializer that stringifies
 * everything or flattens one level.
 */
const CRITERIA = {
    labels: ["abandoned", "industrial"],
    security: { max: 3, has_cameras: false },
    visited: true,
    within_km: 12.5,
};

test.describe("saved filters", () => {
    test("a filter can be created, listed, renamed and deleted", async ({ api }) => {
        const name = resourceName("filter lifecycle");
        const created = await api.json<SavedFilter>("post", "saved-filters/", { name, criteria: CRITERIA });
        expect(created.uuid, `the created filter carries no uuid: ${JSON.stringify(created).slice(0, 200)}`).toBeTruthy();
        api.track("saved-filter", created.uuid, () => api.delete(`saved-filters/${created.uuid}/`));

        const page = await api.json<Page<SavedFilter>>("get", "saved-filters/", { page_size: "100" });
        expect(page.results.some((filter) => filter.uuid === created.uuid), "a filter just created is missing from the list").toBeTruthy();

        const renamed = `${name} edited`;
        const patched = await api.patch(`saved-filters/${created.uuid}/`, { name: renamed });
        expect(patched.status(), `PATCH answered ${patched.status()}: ${(await patched.text()).slice(0, 200)}`).toBe(200);
        expect((await api.json<SavedFilter>("get", `saved-filters/${created.uuid}/`)).name).toBe(renamed);

        const removed = await api.delete(`saved-filters/${created.uuid}/`);
        expect(removed.ok(), `DELETE answered ${removed.status()}`).toBeTruthy();
        expect((await api.get(`saved-filters/${created.uuid}/`)).status()).toBe(404);
    });

    test("criteria survive the round trip exactly", async ({ api }) => {
        const created = await api.json<SavedFilter>("post", "saved-filters/", { name: resourceName("criteria fidelity"), criteria: CRITERIA });
        api.track("saved-filter", created.uuid, () => api.delete(`saved-filters/${created.uuid}/`));

        const readBack = await api.json<SavedFilter>("get", `saved-filters/${created.uuid}/`);

        // Deep equality, not a substring check. A smart list recomputes its
        // membership from this object, so a number arriving back as "12.5" or a
        // nested object flattened into dotted keys changes which pins are in
        // somebody's list without anything looking broken.
        expect(readBack.criteria, `criteria did not survive storage. Sent ${JSON.stringify(CRITERIA)}, got back ${JSON.stringify(readBack.criteria)}`).toEqual(CRITERIA);
    });

    test("an empty criteria object is allowed", async ({ api }) => {
        // The "everything" filter. Rejecting it would make the natural starting
        // state impossible to save.
        const response = await api.post("saved-filters/", { name: resourceName("empty criteria"), criteria: {} });
        expect(response.status(), `an empty criteria object answered ${response.status()}: ${(await response.text()).slice(0, 200)}`).toBeLessThan(300);

        const created = (await response.json()) as SavedFilter;
        api.track("saved-filter", created.uuid, () => api.delete(`saved-filters/${created.uuid}/`));
    });

    test("criteria that are not an object are refused", async ({ api }) => {
        const response = await api.post("saved-filters/", { name: resourceName("bad criteria"), criteria: "not an object" });

        expect(response.status(), `a string criteria answered ${response.status()}`).toBe(400);
        expect(await response.json()).toHaveProperty("error");
    });

    test("a filter needs a name", async ({ api }) => {
        const response = await api.post("saved-filters/", { criteria: CRITERIA });
        expect(response.status()).toBe(400);
        expect(await response.json()).toHaveProperty("error");
    });

    test("a filter uuid that belongs to nobody is refused", async ({ api }) => {
        const response = await api.get("saved-filters/00000000-0000-4000-8000-000000000000/");
        expect(response.status()).toBe(404);
        expect(await response.json()).toHaveProperty("error");
    });
});
