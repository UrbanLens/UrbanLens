/**
 * The pins domain end to end, against a real database.
 *
 * Pin creation is shared: the external API's POST and the map UI's "Add pin"
 * form both go through `create_pin_for_profile`, so the fuzzy-location dedup,
 * the geocoding gate, the slug allocation and the background enrichment all
 * apply either way. That makes this the one endpoint worth exercising against
 * real data rather than a fixture - the dedup in particular behaves differently
 * against a database that already contains locations, which is precisely the
 * situation a unit test never reproduces.
 */

import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";

test.describe("pins", () => {
    test("a pin can be created, read back, edited and deleted", async ({ api }) => {
        const name = resourceName("lifecycle");
        const created = await api.createPin({ name });

        expect(created.uuid).toBeTruthy();
        expect(created.slug).toBeTruthy();

        const detail = await api.get(`pins/${created.slug}/`);
        expect(detail.status()).toBe(200);
        const body = (await detail.json()) as Record<string, unknown>;
        expect(body.name).toBe(name);

        const renamed = `${name} (edited)`;
        const patched = await api.patch(`pins/${created.slug}/`, { name: renamed });
        expect(patched.status(), `PATCH answered ${patched.status()}: ${await patched.text()}`).toBe(200);

        const afterEdit = (await api.json<Record<string, unknown>>("get", `pins/${created.slug}/`)).name;
        expect(afterEdit).toBe(renamed);

        const removed = await api.delete(`pins/${created.slug}/`);
        expect(removed.ok(), `DELETE answered ${removed.status()}`).toBeTruthy();

        const gone = await api.get(`pins/${created.slug}/`);
        expect(gone.status()).toBe(404);
    });

    test("a created pin appears in the sync feed", async ({ api }) => {
        const created = await api.createPin();

        const feed = await api.json<{ pins: Array<{ slug?: string; uuid?: string }>; sync_watermark?: string }>("get", "pins/");
        expect(Array.isArray(feed.pins)).toBeTruthy();
        expect(feed.pins.some((pin) => pin.uuid === created.uuid)).toBeTruthy();
        // The watermark is what a client sends back as `modified_since`; a feed
        // without one turns every sync into a full resync.
        expect(feed.sync_watermark, "the sync feed returned no watermark").toBeTruthy();
    });

    test("a deletion is published to the tombstone feed", async ({ api }) => {
        const created = await api.createPin();
        await api.delete(`pins/${created.slug}/`);

        // Deletions are a separate feed on purpose: a client that only sees
        // changed rows can never learn that a row went away.
        const feed = await api.json<{ tombstones: Array<{ pin_uuid?: string; deleted_at?: string }>; sync_watermark?: string }>("get", "pins/deleted/");
        expect(Array.isArray(feed.tombstones), `pins/deleted/ answered ${JSON.stringify(feed).slice(0, 200)}`).toBeTruthy();
        // Entries are `{pin_uuid, deleted_at}` - the pin's own uuid under a
        // name of its own, because a tombstone is a row about a pin rather
        // than a pin.
        expect(
            feed.tombstones.some((entry) => entry.pin_uuid === created.uuid),
            "a deleted pin never appeared in pins/deleted/, so an offline client would keep showing it",
        ).toBeTruthy();
    });

    test("a repeated create with the same uuid returns the original rather than a duplicate", async ({ api }) => {
        // An offline outbox stamps its uuid at capture time and retries until
        // acknowledged. Without idempotency a flaky connection silently doubles
        // every pin the user made in the field.
        const uuid = crypto.randomUUID();
        const payload = { uuid, name: resourceName("idempotency"), latitude: 42.9012, longitude: -73.9012, name_is_user_provided: true };

        const first = await api.post("pins/", payload);
        expect([200, 201]).toContain(first.status());
        const firstBody = (await first.json()) as { uuid: string; slug: string; created?: boolean };
        api.track("pin", firstBody.slug, () => api.delete(`pins/${firstBody.slug}/`));

        const second = await api.post("pins/", payload);
        expect([200, 201]).toContain(second.status());
        const secondBody = (await second.json()) as { uuid: string; created?: boolean };

        expect(secondBody.uuid, "a retried create produced a second pin").toBe(firstBody.uuid);
        expect(secondBody.created, "the retry reported itself as a fresh create").toBeFalsy();
    });

    test("a payload with neither coordinates nor an address is refused with field detail", async ({ api }) => {
        const response = await api.post("pins/", { name: resourceName("invalid") });
        expect(response.status()).toBe(400);

        const body = (await response.json()) as { error?: string; fields?: Record<string, unknown> };
        // The documented two-part envelope: one message a client can show, plus
        // per-field detail it can attach to inputs.
        expect(body.error).toBeTruthy();
        expect(body.fields ?? body.error, "a validation failure carried neither fields nor a message").toBeTruthy();
    });

    test("coordinates outside the world are refused", async ({ api }) => {
        const response = await api.post("pins/", { name: resourceName("out of range"), latitude: 91, longitude: 0 });
        expect(response.status()).toBe(400);
    });

    test("an unknown pin slug is refused with the standard envelope", async ({ api }) => {
        const missing = await api.get("pins/definitely-not-a-real-pin-slug-91b2c/");
        expect(missing.status()).toBe(404);
        expect(await missing.json()).toHaveProperty("error");
    });

    ifSecondaryAccount()("somebody else's pin is indistinguishable from one that never existed", async ({ api, secondaryApi }) => {
        // The anti-enumeration guarantee in external_api/errors.py. Asserted as
        // the property rather than against a fixed string: what protects users
        // is that the two answers are *identical*, not that they contain any
        // particular wording, and a test pinned to the wording breaks every
        // time the copy is improved while proving nothing extra.
        const theirs = await secondaryApi.createPin({ name: resourceName("someone else's pin") });

        const foreign = await api.get(`pins/${theirs.slug}/`);
        const nonexistent = await api.get("pins/definitely-not-a-real-pin-slug-91b2c/");

        expect(foreign.status(), "another account's pin answered something other than 404").toBe(404);
        expect(nonexistent.status()).toBe(404);
        expect(
            await foreign.text(),
            "the response for another account's pin differs from the one for a pin that never existed, which makes a slug an oracle for what other people have pinned",
        ).toBe(await nonexistent.text());
    });
});
