/**
 * A key is only allowed to do what its scopes say.
 *
 * Every other spec checks scope enforcement for the one domain it covers, which
 * means each check is written by whoever was thinking about that domain. This
 * one asks the same question of the whole write surface at once, because the
 * failure worth catching is not "labels forgot its scope" - it is that *one*
 * endpoint out of forty is different, and nothing that walks a single domain
 * will find it.
 *
 * Driven with the restricted key the provisioning command mints alongside the
 * full one: it carries `profile:read` and nothing else. A valid credential that
 * is insufficient is the only way to tell enforcement from absence - an
 * unauthenticated request is refused by authentication, and proves nothing
 * about scopes at all.
 *
 * Two endpoints are deliberately absent. Messaging has its own file, because it
 * is closed to API keys entirely rather than by scope. And nothing here writes
 * with the *full* key: the point is the refusal, and a sweep that created forty
 * rows to prove the other half would cost more than it tells you.
 */

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import type { ApiClient } from "../../lib/api-client.js";

/** One write a `profile:read` key must not be allowed to perform. */
interface Write {
    /** What the endpoint is, for the failure message. */
    what: string;
    method: "post" | "patch" | "put" | "delete";
    path: string;
    body?: unknown;
}

/**
 * A representative write from every domain the API exposes.
 *
 * Paths that need an existing object use one that does not exist. That is
 * deliberate and is the sharper test: authorization has to be decided *before*
 * the object is looked up, so a refused caller cannot use the difference
 * between 403 and 404 to find out what exists.
 */
const WRITES: Write[] = [
    { what: "create a pin", method: "post", path: "pins/", body: { name: "should not be created", latitude: 42.65, longitude: -73.75 } },
    { what: "edit a pin", method: "patch", path: "pins/no-such-pin-91b2c/", body: { name: "nope" } },
    { what: "delete a pin", method: "delete", path: "pins/no-such-pin-91b2c/" },
    { what: "create a label", method: "post", path: "labels/", body: { name: "should not be created", kind: "tag" } },
    { what: "create a list", method: "post", path: "lists/", body: { name: "should not be created" } },
    { what: "create a trip", method: "post", path: "trips/", body: { name: "should not be created" } },
    { what: "log a visit", method: "post", path: "pins/no-such-pin-91b2c/visits/", body: { visited_at: new Date(Date.now() - 3600_000).toISOString() } },
    { what: "add a pin note", method: "post", path: "pins/no-such-pin-91b2c/notes/", body: { text: "nope" } },
    { what: "add a pin link", method: "post", path: "pins/no-such-pin-91b2c/links/", body: { url: "https://example.invalid/" } },
    { what: "comment on a pin", method: "post", path: "pins/no-such-pin-91b2c/comments/", body: { text: "nope" } },
    { what: "define a custom field", method: "post", path: "custom-fields/", body: { name: "should not be created", entity_type: "pin" } },
    { what: "create a saved filter", method: "post", path: "saved-filters/", body: { name: "should not be created" } },
    { what: "open a safety check-in", method: "post", path: "safety/checkins/", body: { checkin_by: new Date(Date.now() + 3600_000).toISOString() } },
    { what: "change safety settings", method: "patch", path: "safety/settings/", body: { default_grace_period_seconds: 60 } },
    { what: "send a friend request", method: "post", path: "friends/", body: { profile_uuid: "00000000-0000-4000-8000-000000000000" } },
    { what: "register a push device", method: "post", path: "push-devices/", body: { address: "should-not-register", transport: "fcm" } },
    { what: "edit a wiki", method: "patch", path: "wikis/no-such-location-91b2c/", body: { description: "nope" } },
    { what: "comment on a wiki", method: "post", path: "wikis/no-such-location-91b2c/comments/", body: { text: "nope" } },
    { what: "restore an undo entry", method: "post", path: "undo/00000000-0000-4000-8000-000000000000/restore/", body: {} },
    { what: "mark a notification read", method: "post", path: "notifications/00000000-0000-4000-8000-000000000000/", body: {} },
    { what: "mark all notifications read", method: "post", path: "notifications/read-all/", body: {} },
];

/** Reads a `profile:read` key is entitled to nothing from. */
const READS = [
    { what: "list pins", path: "pins/" },
    { what: "list labels", path: "labels/" },
    { what: "list trips", path: "trips/" },
    { what: "list photos", path: "photos/" },
    { what: "list notifications", path: "notifications/" },
    { what: "read the undo feed", path: "undo/" },
    { what: "search", path: "search/" },
    { what: "list custom fields", path: "custom-fields/" },
    { what: "list saved filters", path: "saved-filters/" },
    { what: "list safety check-ins", path: "safety/checkins/" },
];

async function send(api: ApiClient, write: Write) {
    if (write.method === "delete") {
        return api.delete(write.path);
    }
    return api[write.method](write.path, write.body);
}

test.describe("scope enforcement", () => {
    test("a profile:read key cannot write anywhere", async ({ restrictedApi }) => {
        const allowed: string[] = [];

        for (const write of WRITES) {
            const response = await send(restrictedApi, write);
            if (response.status() !== 403) {
                allowed.push(`${write.what}: ${write.method.toUpperCase()} ${write.path} -> ${response.status()}`);
            }
        }

        expect(
            allowed,
            `a key carrying only profile:read was not refused by these writes:\n  ${allowed.join("\n  ")}\n` +
                "A 404 here is as much a problem as a 200: it means the object was looked up before the caller's authority was, " +
                "so the refusal doubles as a way to find out what exists.",
        ).toHaveLength(0);
    });

    test("a profile:read key cannot read other domains", async ({ restrictedApi }) => {
        const allowed: string[] = [];

        for (const read of READS) {
            const response = await restrictedApi.get(read.path);
            if (response.status() !== 403) {
                allowed.push(`${read.what}: GET ${read.path} -> ${response.status()}`);
            }
        }

        expect(allowed, `a key carrying only profile:read could read:\n  ${allowed.join("\n  ")}`).toHaveLength(0);
    });

    test("the restricted key is genuinely valid, so the refusals mean something", async ({ restrictedApi }) => {
        // Without this the whole file could pass against a revoked or malformed
        // key - everything would be refused, and for the wrong reason. The one
        // scope it *does* carry has to work.
        const response = await restrictedApi.get("whoami/");

        expect(
            response.status(),
            `the restricted key cannot even identify itself (${response.status()}), so every refusal above may be authentication rather than scope`,
        ).toBe(200);
    });

    test("the full key can do what the restricted one cannot", async ({ api }) => {
        // The other half of the same argument: if the full key were also
        // refused everywhere, the refusals would be telling us nothing about
        // scopes. One cheap write is enough to establish the contrast.
        const label = await api.post("labels/", { name: resourceName("scope contrast"), kind: "tag" });
        expect(label.status(), `the full key could not create a label (${label.status()}), so the refusals above are not about scope`).toBeLessThan(300);

        const created = (await label.json()) as { uuid?: string };
        if (created.uuid) {
            api.track("label", created.uuid, () => api.delete(`labels/${created.uuid}/`));
        }
    });
});
