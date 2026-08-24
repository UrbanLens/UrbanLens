/**
 * Labels: the lifecycle, and the two behaviours that only show up against data.
 *
 * The counts are the interesting part. `pin_count`/`location_count` are opt-in
 * (`?with_counts=true`) because each costs a correlated subquery per label, and
 * that opt-in is exactly the kind of thing that drifts from its published
 * schema: the document declared both fields *required* while the response
 * omitted them unless asked (docs/PROBLEMS.md, 2026-08-24). Asserting both
 * halves - absent by default, present and correct when requested - is what
 * keeps the endpoint and its contract honest about the same thing.
 */

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";

/** The paging envelope every browse endpoint uses. */
interface Page<T> {
    count: number;
    next: string | null;
    previous: string | null;
    results: T[];
}

interface Label {
    uuid: string;
    name: string;
    kind: string;
    pin_count?: number;
    location_count?: number;
}

test.describe("labels", () => {
    test("a label can be created, listed, renamed and deleted", async ({ api }) => {
        const name = resourceName("label lifecycle");
        const created = await api.json<Label>("post", "labels/", { name, kind: "tag" });
        expect(created.uuid).toBeTruthy();
        api.track("label", created.uuid, () => api.delete(`labels/${created.uuid}/`));

        const page = await api.json<Page<Label>>("get", "labels/", { page_size: "100" });
        expect(page.results.some((label) => label.uuid === created.uuid), "a label just created is missing from the list").toBeTruthy();

        const renamed = `${name} edited`;
        const patched = await api.patch(`labels/${created.uuid}/`, { name: renamed });
        expect(patched.status(), `PATCH answered ${patched.status()}: ${await patched.text()}`).toBe(200);
        expect((await api.json<Label>("get", `labels/${created.uuid}/`)).name).toBe(renamed);

        const removed = await api.delete(`labels/${created.uuid}/`);
        expect(removed.ok(), `DELETE answered ${removed.status()}`).toBeTruthy();
        expect((await api.get(`labels/${created.uuid}/`)).status()).toBe(404);
    });

    test("counts are omitted by default and correct when asked for", async ({ api }) => {
        const label = await api.json<Label>("post", "labels/", { name: resourceName("counted"), kind: "tag" });
        api.track("label", label.uuid, () => api.delete(`labels/${label.uuid}/`));

        const bare = await api.json<Label>("get", `labels/${label.uuid}/`);
        // Absent, not zero: a client that renders "0 pins" for a count it never
        // asked for is showing a number the server did not compute.
        expect(bare, "pin_count was returned without ?with_counts=true, so every list pays for a subquery per label").not.toHaveProperty("pin_count");
        expect(bare).not.toHaveProperty("location_count");

        const pin = await api.createPin({ name: resourceName("labelled pin") });
        const applied = await api.patch(`pins/${pin.slug}/`, { label_uuids: [label.uuid] });
        expect(applied.status(), `applying the label answered ${applied.status()}: ${await applied.text()}`).toBe(200);

        const counted = await api.json<Label>("get", `labels/${label.uuid}/`, { with_counts: "true" });
        expect(counted.pin_count, "with_counts=true did not produce a pin_count").toBeDefined();
        expect(counted.pin_count, "the label is on one pin but does not count it").toBe(1);
    });

    test("a label cannot be created without a name", async ({ api }) => {
        const response = await api.post("labels/", { kind: "tag" });
        expect(response.status()).toBe(400);
        expect(await response.json()).toHaveProperty("error");
    });

    test("two labels cannot share a name", async ({ api }) => {
        // Unique on (lower(name), profile, kind) - and the uniqueness check is
        // deliberately done in the service rather than left to the database,
        // because reaching the constraint is a 500. Only a real second insert
        // proves the service check is in front of it.
        const name = resourceName("duplicate");
        const first = await api.json<Label>("post", "labels/", { name, kind: "tag" });
        api.track("label", first.uuid, () => api.delete(`labels/${first.uuid}/`));

        const second = await api.post("labels/", { name: name.toUpperCase(), kind: "tag" });
        expect(second.status(), "a case variant of an existing label name was accepted, or reached the database constraint as a 500").toBe(400);
        expect(await second.json()).toHaveProperty("error");
    });

    test("a key without the labels scope cannot write", async ({ restrictedApi }) => {
        const response = await restrictedApi.post("labels/", { name: resourceName("forbidden"), kind: "tag" });
        expect(response.status(), "a profile:read key created a label").toBe(403);
    });
});
