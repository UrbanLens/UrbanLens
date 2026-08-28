/**
 * Object-level authorization across the published API.
 *
 * Pins are private by construction. So are lists, trips, labels, saved
 * filters, custom fields, photos, notes, visits, safety check-ins and undo
 * entries. Each of those collections re-implements "is this the caller's?",
 * and the failure worth catching is that *one* of them looks the object up
 * before it looks the caller up - a 200 or a 403 where a 404 belongs, or a
 * write that lands in somebody else's row.
 *
 * Every case has three legs:
 *   1. the owner can read (or write) their own object - otherwise a 404 is
 *      just a broken endpoint;
 *   2. a second account cannot;
 *   3. the second account's answer is identical to the answer for an object
 *      that never existed, so a slug is not an oracle.
 */

import type { APIResponse } from "@playwright/test";

import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import { appRoutes } from "../../lib/routes.js";
import type { ApiClient } from "../../lib/api-client.js";
import { expectIndistinguishableFromMissing, expectNotServerError, MISSING_SLUG, MISSING_UUID, whoami } from "../../lib/security.js";

interface Page<T> {
    count?: number;
    results?: T[];
}

async function createList(api: ApiClient): Promise<{ slug: string; name: string }> {
    const created = await api.json<{ slug: string; name: string }>("post", "lists/", {
        name: resourceName("sec list"),
        description: "Owned by the security suite; not for sharing.",
    });
    expect(created.slug, `list create carried no slug: ${JSON.stringify(created)}`).toBeTruthy();
    api.track("list", created.slug, () => api.delete(`lists/${created.slug}/`));
    return created;
}

async function createTrip(api: ApiClient): Promise<{ slug: string; name: string }> {
    const created = await api.json<{ slug: string; name: string }>("post", "trips/", {
        name: resourceName("sec trip"),
        description: "Owned by the security suite; not for sharing.",
    });
    expect(created.slug, `trip create carried no slug: ${JSON.stringify(created)}`).toBeTruthy();
    api.track("trip", created.slug, () => api.delete(`trips/${created.slug}/`));
    return created;
}

async function createLabel(api: ApiClient): Promise<{ uuid: string; name: string }> {
    const created = await api.json<{ uuid: string; name: string }>("post", "labels/", {
        name: resourceName("sec label"),
        kind: "tag",
    });
    expect(created.uuid).toBeTruthy();
    api.track("label", created.uuid, () => api.delete(`labels/${created.uuid}/`));
    return created;
}

async function createFilter(api: ApiClient): Promise<{ uuid: string; name: string }> {
    const created = await api.json<{ uuid: string; name: string }>("post", "saved-filters/", {
        name: resourceName("sec filter"),
        criteria: { security: { max: 1 } },
    });
    expect(created.uuid).toBeTruthy();
    api.track("saved-filter", created.uuid, () => api.delete(`saved-filters/${created.uuid}/`));
    return created;
}

async function createCustomField(api: ApiClient): Promise<{ id: string | number; name: string }> {
    const created = await api.json<{ id?: number; field_id?: number; uuid?: string; name: string }>("post", "custom-fields/", {
        name: resourceName("sec field"),
        entity_type: "pin",
        field_type: "text",
    });
    const id = created.id ?? created.field_id ?? created.uuid;
    expect(id, `custom field carried no identifier: ${JSON.stringify(created)}`).toBeTruthy();
    const identifier = id as string | number;
    api.track("custom-field", String(identifier), () => api.delete(`custom-fields/${identifier}/`));
    return { id: identifier, name: created.name };
}

test.describe("cross-account reads look like absence", () => {
    ifSecondaryAccount()("another account's pin", async ({ api, secondaryApi }) => {
        const pin = await api.createPin({ name: resourceName("owner-only pin") });

        const mine = await api.get(`pins/${pin.slug}/`);
        expect(mine.status(), "the owner could not read their pin, so the stranger's 404 would prove nothing").toBe(200);

        await expectIndistinguishableFromMissing(
            await secondaryApi.get(`pins/${pin.slug}/`),
            await secondaryApi.get(`pins/${MISSING_SLUG}/`),
            "another account's pin",
        );
    });

    ifSecondaryAccount()("another account's list", async ({ api, secondaryApi }) => {
        const list = await createList(api);
        expect((await api.get(`lists/${list.slug}/`)).status()).toBe(200);
        await expectIndistinguishableFromMissing(
            await secondaryApi.get(`lists/${list.slug}/`),
            await secondaryApi.get(`lists/${MISSING_SLUG}/`),
            "another account's list",
        );
    });

    ifSecondaryAccount()("another account's trip", async ({ api, secondaryApi }) => {
        const trip = await createTrip(api);
        expect((await api.get(`trips/${trip.slug}/`)).status()).toBe(200);
        await expectIndistinguishableFromMissing(
            await secondaryApi.get(`trips/${trip.slug}/`),
            await secondaryApi.get(`trips/${MISSING_SLUG}/`),
            "another account's trip",
        );
    });

    ifSecondaryAccount()("another account's label", async ({ api, secondaryApi }) => {
        const label = await createLabel(api);
        expect((await api.get(`labels/${label.uuid}/`)).status()).toBe(200);
        await expectIndistinguishableFromMissing(
            await secondaryApi.get(`labels/${label.uuid}/`),
            await secondaryApi.get(`labels/${MISSING_UUID}/`),
            "another account's label",
        );
    });

    ifSecondaryAccount()("another account's saved filter", async ({ api, secondaryApi }) => {
        const filter = await createFilter(api);
        expect((await api.get(`saved-filters/${filter.uuid}/`)).status()).toBe(200);
        await expectIndistinguishableFromMissing(
            await secondaryApi.get(`saved-filters/${filter.uuid}/`),
            await secondaryApi.get(`saved-filters/${MISSING_UUID}/`),
            "another account's saved filter",
        );
    });

    ifSecondaryAccount()("another account's custom field", async ({ api, secondaryApi }) => {
        const field = await createCustomField(api);
        expect((await api.get(`custom-fields/${field.id}/`)).status()).toBe(200);
        await expectIndistinguishableFromMissing(
            await secondaryApi.get(`custom-fields/${field.id}/`),
            await secondaryApi.get("custom-fields/999999991/"),
            "another account's custom field",
        );
    });
});

test.describe("cross-account writes do not land", () => {
    ifSecondaryAccount()("another account cannot rename a pin", async ({ api, secondaryApi }) => {
        const pin = await api.createPin({ name: resourceName("untouchable pin") });
        const original = pin.name;

        const patched = await secondaryApi.patch(`pins/${pin.slug}/`, { name: "hijacked by another account" });
        await expectNotServerError(patched, "PATCH on another account's pin");
        expect(patched.status(), `another account renamed a pin (${patched.status()})`).not.toBeLessThan(300);

        const still = await api.json<{ name: string }>("get", `pins/${pin.slug}/`);
        expect(still.name, "another account's refused PATCH still changed the pin").toBe(original);
    });

    ifSecondaryAccount()("another account cannot delete a pin", async ({ api, secondaryApi }) => {
        const pin = await api.createPin({ name: resourceName("not deletable") });

        const removed = await secondaryApi.delete(`pins/${pin.slug}/`);
        await expectNotServerError(removed, "DELETE on another account's pin");
        expect(removed.status(), `another account deleted a pin (${removed.status()})`).not.toBeLessThan(300);

        expect((await api.get(`pins/${pin.slug}/`)).status(), "the pin vanished after a stranger's DELETE was refused").toBe(200);
    });

    ifSecondaryAccount()("another account cannot rename a list, trip, label or filter", async ({ api, secondaryApi }) => {
        const list = await createList(api);
        const trip = await createTrip(api);
        const label = await createLabel(api);
        const filter = await createFilter(api);

        const attempts: Array<{ what: string; send: () => Promise<APIResponse>; reread: () => Promise<string> }> = [
            {
                what: "list",
                send: () => secondaryApi.patch(`lists/${list.slug}/`, { name: "hijacked list" }),
                reread: async () => (await api.json<{ name: string }>("get", `lists/${list.slug}/`)).name,
            },
            {
                what: "trip",
                send: () => secondaryApi.patch(`trips/${trip.slug}/`, { name: "hijacked trip" }),
                reread: async () => (await api.json<{ name: string }>("get", `trips/${trip.slug}/`)).name,
            },
            {
                what: "label",
                send: () => secondaryApi.patch(`labels/${label.uuid}/`, { name: "hijacked label" }),
                reread: async () => (await api.json<{ name: string }>("get", `labels/${label.uuid}/`)).name,
            },
            {
                what: "saved filter",
                send: () => secondaryApi.patch(`saved-filters/${filter.uuid}/`, { name: "hijacked filter" }),
                reread: async () => (await api.json<{ name: string }>("get", `saved-filters/${filter.uuid}/`)).name,
            },
        ];

        const landed: string[] = [];
        for (const attempt of attempts) {
            const response = await attempt.send();
            await expectNotServerError(response, `PATCH on another account's ${attempt.what}`);
            if (response.status() < 300) {
                landed.push(`${attempt.what}: ${response.status()}`);
                continue;
            }
            const name = await attempt.reread();
            if (name.startsWith("hijacked")) {
                landed.push(`${attempt.what}: refused ${response.status()} but the name is now "${name}"`);
            }
        }
        expect(landed, `another account changed someone else's objects:\n  ${landed.join("\n  ")}`).toHaveLength(0);
    });

    ifSecondaryAccount()("another account cannot write notes onto a pin they do not own", async ({ api, secondaryApi }) => {
        const pin = await api.createPin({ name: resourceName("note host") });
        const created = await secondaryApi.post(`pins/${pin.slug}/notes/`, { text: "a note that should never persist" });
        expect(created.status(), `writing a note onto another account's pin answered ${created.status()}`).toBe(404);

        const listed = await api.get(`pins/${pin.slug}/notes/`);
        expect(listed.status()).toBe(200);
        expect(await listed.text(), "a refused note still appeared in the owner's list").not.toContain("a note that should never persist");
    });

    ifSecondaryAccount()("another account cannot log a visit on a pin they do not own", async ({ api, secondaryApi }) => {
        const pin = await api.createPin({ name: resourceName("visit host") });
        const created = await secondaryApi.post(`pins/${pin.slug}/visits/`, {
            visited_at: new Date(Date.now() - 86_400_000).toISOString(),
            notes: "a visit that should never persist",
        });
        expect(created.status(), `logging a visit on another account's pin answered ${created.status()}`).toBe(404);
    });
});

test.describe("collections do not accept another user's objects", () => {
    ifSecondaryAccount()("a list will not take a pin uuid the caller does not own", async ({ api, secondaryApi }) => {
        const theirs = await secondaryApi.createPin({ name: resourceName("foreign list member") });
        const list = await createList(api);

        const added = await api.post(`lists/${list.slug}/items/`, { pin_uuids: [theirs.uuid] });
        await expectNotServerError(added, "adding another account's pin to a list");

        if (added.status() === 200) {
            const body = (await added.json()) as { added?: number };
            expect(body.added, "another account's pin uuid was counted as added to a private list").toBe(0);
        } else {
            expect(added.status(), `adding another account's pin answered ${added.status()}`).toBeGreaterThanOrEqual(400);
        }

        const items = await api.get(`lists/${list.slug}/items/`);
        if (items.status() === 200) {
            expect(await items.text(), "another account's pin is now a member of this list").not.toContain(theirs.uuid);
        }
    });

    ifSecondaryAccount()("a trip activity will not attach a pin the caller does not own", async ({ api, secondaryApi }) => {
        const theirs = await secondaryApi.createPin({ name: resourceName("foreign trip stop") });
        const trip = await createTrip(api);

        const activity = await api.post(`trips/${trip.slug}/activities/`, {
            title: resourceName("should not attach"),
            pin_slug: theirs.slug,
        });
        await expectNotServerError(activity, "attaching another account's pin to a trip");
        expect(activity.status(), `a trip activity accepted another account's pin (${activity.status()})`).toBeGreaterThanOrEqual(400);

        const listed = await api.get(`trips/${trip.slug}/activities/`);
        if (listed.status() === 200) {
            expect(await listed.text(), "the trip's activity list now names another account's pin").not.toContain(theirs.slug);
        }
    });
});

test.describe("indexes never include another account's rows", () => {
    ifSecondaryAccount()("pin sync, lists, trips, labels and filters are each scoped to the caller", async ({ api, secondaryApi }) => {
        const pin = await api.createPin({ name: resourceName("index isolation pin") });
        const list = await createList(api);
        const trip = await createTrip(api);
        const label = await createLabel(api);
        const filter = await createFilter(api);

        const leaks: string[] = [];

        const pinFeed = await secondaryApi.get("pins/");
        expect(pinFeed.status()).toBe(200);
        if ((await pinFeed.text()).includes(pin.uuid)) {
            leaks.push(`pins/ listed ${pin.uuid}`);
        }

        const lists = await secondaryApi.json<Page<{ slug?: string }>>("get", "lists/", { page_size: "100" });
        if ((lists.results ?? []).some((row) => row.slug === list.slug)) {
            leaks.push(`lists/ listed ${list.slug}`);
        }

        const trips = await secondaryApi.json<Page<{ slug?: string }>>("get", "trips/", { page_size: "100" });
        if ((trips.results ?? []).some((row) => row.slug === trip.slug)) {
            leaks.push(`trips/ listed ${trip.slug}`);
        }

        const labels = await secondaryApi.json<Page<{ uuid?: string }>>("get", "labels/", { page_size: "100" });
        if ((labels.results ?? []).some((row) => row.uuid === label.uuid)) {
            leaks.push(`labels/ listed ${label.uuid}`);
        }

        const filters = await secondaryApi.json<Page<{ uuid?: string }>>("get", "saved-filters/", { page_size: "100" });
        if ((filters.results ?? []).some((row) => row.uuid === filter.uuid)) {
            leaks.push(`saved-filters/ listed ${filter.uuid}`);
        }

        expect(leaks, `another account's index included this account's objects:\n  ${leaks.join("\n  ")}`).toHaveLength(0);
    });
});

test.describe("undo cannot restore another account's deletions", () => {
    ifSecondaryAccount()("a stranger cannot restore an entry they cannot see", async ({ api, secondaryApi }) => {
        const name = resourceName("undo isolation");
        const pin = await api.createPin({ name });
        const removed = await api.delete(`pins/${pin.slug}/`);
        expect(removed.ok()).toBeTruthy();

        const feed = await api.json<{ entries: Array<{ uuid: string; object_repr?: string }> }>("get", "undo/");
        const entry = feed.entries.find((candidate) => candidate.object_repr?.includes(name));
        expect(entry, "the owner has no undo entry for a pin they just deleted, so the stranger test has nothing to refuse").toBeTruthy();

        const strangerFeed = await secondaryApi.get("undo/");
        expect(strangerFeed.status()).toBe(200);
        expect(await strangerFeed.text(), "another account's undo feed lists this account's deletion").not.toContain(name);

        const restored = await secondaryApi.post(`undo/${entry?.uuid}/restore/`);
        await expectNotServerError(restored, "restore of another account's undo entry");
        expect(restored.status(), `another account restored a deletion (${restored.status()})`).toBeGreaterThanOrEqual(400);

        // The owner can still restore - the stranger's attempt must not have
        // consumed the entry.
        const ownerRestore = await api.post(`undo/${entry?.uuid}/restore/`);
        expect(ownerRestore.ok(), `the owner could not restore after a stranger tried (${ownerRestore.status()})`).toBeTruthy();
        api.track("pin", pin.slug, () => api.delete(`pins/${pin.slug}/`));
    });
});

test.describe("safety check-ins stay with their owner", () => {
    ifSecondaryAccount()("another account cannot read or cancel a check-in", async ({ api, secondaryApi }) => {
        const created = await api.post("safety/checkins/", {
            title: resourceName("sec check-in"),
            checkin_by: new Date(Date.now() + 120 * 60_000).toISOString(),
        });
        test.skip(
            created.status() === 409,
            "This account already has an active check-in (likely another spec in this run). Re-run in isolation to exercise this case.",
        );
        expect(created.status(), `opening a check-in answered ${created.status()}: ${(await created.text()).slice(0, 200)}`).toBeLessThan(300);
        const checkin = (await created.json()) as { slug?: string };
        expect(checkin.slug).toBeTruthy();
        const slug = String(checkin.slug);

        try {
            await api.post(`safety/checkins/${slug}/cancel/`);

            const mine = await api.get(`safety/checkins/${slug}/`);
            expect(mine.status(), "the owner could not read their own check-in after cancelling it").toBe(200);

            await expectIndistinguishableFromMissing(
                await secondaryApi.get(`safety/checkins/${slug}/`),
                await secondaryApi.get(`safety/checkins/${MISSING_SLUG}/`),
                "another account's safety check-in",
            );

            const cancelled = await secondaryApi.post(`safety/checkins/${slug}/cancel/`);
            expect(cancelled.status(), `another account cancelled a check-in (${cancelled.status()})`).toBeGreaterThanOrEqual(400);
        } finally {
            await api.delete(`safety/checkins/${slug}/`);
        }
    });
});

test.describe("privilege cannot be taken from a settings or profile write", () => {
    test("a settings PATCH cannot mint staff or superuser", async ({ api, page }) => {
        const before = await api.get("settings/");
        expect(before.status(), "GET settings/ is the control that this key may read settings").toBe(200);

        const patched = await api.patch("settings/", {
            is_staff: true,
            is_superuser: true,
            is_active: false,
            user: { is_staff: true, is_superuser: true },
        });
        await expectNotServerError(patched, "mass-assignment PATCH on settings");

        const after = await api.json<Record<string, unknown>>("get", "settings/");
        const blob = JSON.stringify(after);
        expect(blob, "settings now reports is_staff").not.toMatch(/"is_staff"\s*:\s*true/);
        expect(blob, "settings now reports is_superuser").not.toMatch(/"is_superuser"\s*:\s*true/);

        await page.goto(appRoutes.home);
        const admin = await page.request.get("/dashboard/site-admin/", { maxRedirects: 0 });
        expect(
            admin.status(),
            `site-admin became reachable after a settings PATCH (${admin.status()})`,
        ).not.toBe(200);
    });

    test("a profile PATCH cannot reassign the user or grant staff", async ({ api }) => {
        const me = await whoami(api);
        const patched = await api.patch(`profiles/${me.slug}/`, {
            is_staff: true,
            is_superuser: true,
            user: 1,
            uuid: MISSING_UUID,
        });
        await expectNotServerError(patched, "mass-assignment PATCH on profile");

        const after = await api.json<Record<string, unknown>>("get", `profiles/${me.slug}/`);
        expect(JSON.stringify(after), "a profile PATCH accepted is_staff").not.toMatch(/"is_staff"\s*:\s*true/);
        const identified = await whoami(api);
        expect(identified.uuid, "a profile PATCH changed which account this key is").toBe(me.uuid);
    });

    ifSecondaryAccount()("a pin PATCH cannot reassign the pin to another profile", async ({ api, secondaryApi }) => {
        const pin = await api.createPin({ name: resourceName("ownership pin") });
        const them = await whoami(secondaryApi);

        const patched = await api.patch(`pins/${pin.slug}/`, { profile: them.uuid, profile_uuid: them.uuid, user: them.uuid });
        expect(patched.status(), "the owner should still be able to PATCH their pin; extra fields are ignored").toBeLessThan(300);

        const stranger = await secondaryApi.get(`pins/${pin.slug}/`);
        expect(stranger.status(), "the pin is now readable by the profile it was 'reassigned' to").toBe(404);

        expect((await api.get(`pins/${pin.slug}/`)).status(), "the original owner lost the pin after a no-op reassignment").toBe(200);
    });
});
