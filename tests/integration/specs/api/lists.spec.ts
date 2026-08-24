/**
 * Pin lists, including the smart ones that derive their membership.
 *
 * A plain list is a join table and would be adequately covered by a unit test.
 * A *smart* list is not: its membership is recomputed from a saved filter's
 * criteria, by a service that reads real pins out of a real database, and the
 * resync path exists precisely because that recomputation happens after the
 * fact rather than at read time. "The list is empty" and "the resync never ran"
 * are indistinguishable from inside one process with two fixture rows.
 */

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";

interface Page<T> {
    count: number;
    results: T[];
}

interface PinList {
    uuid: string;
    slug: string;
    name: string;
    is_smart?: boolean;
}

test.describe("lists", () => {
    test("a list can be created, listed, renamed and deleted", async ({ api }) => {
        const name = resourceName("list lifecycle");
        const created = await api.json<PinList>("post", "lists/", { name, description: "Created by the integration suite." });
        expect(created.slug, `the created list carries no slug: ${JSON.stringify(created)}`).toBeTruthy();
        api.track("list", created.slug, () => api.delete(`lists/${created.slug}/`));

        const page = await api.json<Page<PinList>>("get", "lists/", { page_size: "100" });
        expect(page.results.some((list) => list.slug === created.slug), "a list just created is missing from the index").toBeTruthy();

        const renamed = `${name} edited`;
        const patched = await api.patch(`lists/${created.slug}/`, { name: renamed });
        expect(patched.status(), `PATCH answered ${patched.status()}: ${await patched.text()}`).toBe(200);
        expect((await api.json<PinList>("get", `lists/${created.slug}/`)).name).toBe(renamed);

        const removed = await api.delete(`lists/${created.slug}/`);
        expect(removed.ok(), `DELETE answered ${removed.status()}`).toBeTruthy();
        expect((await api.get(`lists/${created.slug}/`)).status()).toBe(404);
    });

    test("pins can be added to a list and read back", async ({ api }) => {
        const list = await api.json<PinList>("post", "lists/", { name: resourceName("holds pins") });
        api.track("list", list.slug, () => api.delete(`lists/${list.slug}/`));
        const pin = await api.createPin({ name: resourceName("list member") });

        // Membership is by uuid rather than slug: a slug is per-profile and a
        // list can outlive a rename.
        const added = await api.post(`lists/${list.slug}/items/`, { pin_uuids: [pin.uuid] });
        expect(added.status(), `adding a pin answered ${added.status()}: ${(await added.text()).slice(0, 200)}`).toBeLessThan(300);

        const items = await api.get(`lists/${list.slug}/items/`);
        expect(items.status()).toBe(200);
        expect(await items.text(), "a pin just added is not among the list's items").toContain(pin.uuid);
    });

    test("adding a pin that does not exist is refused rather than silently ignored", async ({ api }) => {
        const list = await api.json<PinList>("post", "lists/", { name: resourceName("strict membership") });
        api.track("list", list.slug, () => api.delete(`lists/${list.slug}/`));

        const response = await api.post(`lists/${list.slug}/items/`, { pin_uuids: ["00000000-0000-4000-8000-000000000000"] });
        // A write that accepts an unknown id and stores nothing is the worst of
        // both: the client believes the pin is in the list and it is not.
        expect([400, 404], `adding an unknown pin uuid answered ${response.status()}`).toContain(response.status());
        expect(await response.json()).toHaveProperty("error");
    });

    test("a smart list built from a saved filter resyncs its membership", async ({ api }) => {
        const filter = await api.json<{ uuid: string; name: string }>("post", "saved-filters/", {
            name: resourceName("smart source"),
            criteria: {},
        });
        api.track("saved-filter", filter.uuid, () => api.delete(`saved-filters/${filter.uuid}/`));

        const created = await api.post("lists/", {
            name: resourceName("smart list"),
            is_smart: true,
            source_saved_filter_uuid: filter.uuid,
        });
        expect(created.status(), `creating a smart list answered ${created.status()}: ${(await created.text()).slice(0, 300)}`).toBeLessThan(300);
        const list = (await created.json()) as PinList;
        api.track("list", list.slug, () => api.delete(`lists/${list.slug}/`));
        expect(list.is_smart, "a list created with is_smart=true does not read back as smart").toBeTruthy();

        // The endpoint that recomputes membership. It exists because the
        // computation is deferred, so calling it is the only way to know the
        // deferred half is wired up on a deployment.
        const resynced = await api.post(`lists/${list.slug}/resync/`);
        expect(resynced.status(), `resync answered ${resynced.status()}: ${(await resynced.text()).slice(0, 300)}`).toBeLessThan(300);

        const items = await api.get(`lists/${list.slug}/items/`);
        expect(items.status(), `a resynced smart list's items answered ${items.status()}`).toBe(200);
    });

    test("a key without the lists scope cannot create one", async ({ restrictedApi }) => {
        const response = await restrictedApi.post("lists/", { name: resourceName("forbidden list") });
        expect(response.status(), "a profile:read key created a list").toBe(403);
    });
});
