/**
 * Friendship, driven from both sides at once.
 *
 * The only domain in the suite that genuinely needs two accounts, and the
 * reason it belongs here rather than in a fixture: a friendship is a single row
 * that two people see differently. The requester sees "sent", the recipient
 * sees "received", and every state change - accept, mute, block - has to be
 * observed from the other seat to be worth anything. A unit test can assert both
 * halves against the same in-process object and be wrong about which side the
 * row is stored on; this cannot.
 *
 * `next_cursor`/`results` rather than the `count`/`next`/`previous`/`results`
 * envelope the rest of the API uses, deliberately: the friend list is a feed a
 * client pages through, not a table it jumps around in.
 */

import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import type { ApiClient } from "../../lib/api-client.js";

/** One entry in the friend feed. The other party is nested, not flattened. */
interface FriendEntry {
    profile?: { uuid?: string; slug?: string };
    status?: string;
    direction?: string;
    is_muted?: boolean;
    message?: string;
}

/** The friend feed's envelope. */
interface FriendFeed {
    next_cursor: string | null;
    results: FriendEntry[];
}

/**
 * Statuses the feed will filter by.
 *
 * Capitalised, and that matters: `?status=pending` is not the same string and
 * quietly returns the unfiltered feed. The values come from the schema's enum.
 */
const PENDING = "Pending";

/** The calling credential's own profile. */
async function whoami(api: ApiClient): Promise<{ uuid: string; slug: string }> {
    return api.json<{ uuid: string; slug: string }>("get", "whoami/");
}

/** Finds the entry describing `profileUuid`, if the feed carries one. */
function entryFor(feed: FriendFeed, profileUuid: string): FriendEntry | undefined {
    return feed.results.find((entry) => entry.profile?.uuid === profileUuid);
}

/**
 * The feed, optionally narrowed to one status.
 *
 * The default feed does not include incoming requests - a pending request is
 * not yet a friendship - so anything looking for one has to ask for it.
 */
async function friends(api: ApiClient, status?: string): Promise<FriendFeed> {
    return api.json<FriendFeed>("get", "friends/", status ? { status } : undefined);
}

/** Removes any friendship between the two accounts, ignoring "there wasn't one". */
async function unfriend(api: ApiClient, profileUuid: string): Promise<void> {
    await api.delete(`friends/${profileUuid}/`);
}

test.describe("friendships", () => {
    ifSecondaryAccount()("a request is visible to the recipient, and acceptance to both", async ({ api, secondaryApi }) => {
        const me = await whoami(api);
        const them = await whoami(secondaryApi);
        await unfriend(api, them.uuid);

        const requested = await api.post("friends/", { profile_uuid: them.uuid, message: "Sent by the UrbanLens integration suite." });
        expect(requested.status(), `sending a friend request answered ${requested.status()}: ${(await requested.text()).slice(0, 200)}`).toBeLessThan(300);

        try {
            // The recipient's view. A request that only the sender can see is
            // the failure mode worth catching: it looks sent and never arrives.
            const inbound = await friends(secondaryApi, PENDING);
            expect(
                entryFor(inbound, me.uuid),
                `the recipient's pending feed does not mention the requester. Entries: ${JSON.stringify(inbound.results).slice(0, 300)}`,
            ).toBeTruthy();

            const accepted = await secondaryApi.post(`friends/${me.uuid}/accept/`);
            expect(accepted.status(), `accepting answered ${accepted.status()}: ${(await accepted.text()).slice(0, 200)}`).toBeLessThan(300);

            // And now from the requester's seat, which is the half that a
            // one-sided implementation gets wrong.
            const outbound = await friends(api);
            const entry = entryFor(outbound, them.uuid);
            expect(entry, "after acceptance the requester's feed no longer lists the friend").toBeTruthy();
            expect(String(entry?.status ?? "").toLowerCase(), `the friendship reads as "${entry?.status}" to the requester after being accepted`).toContain("accept");
        } finally {
            await unfriend(api, them.uuid);
        }
    });

    ifSecondaryAccount()("a rejected request does not become a friendship", async ({ api, secondaryApi }) => {
        const me = await whoami(api);
        const them = await whoami(secondaryApi);
        await unfriend(api, them.uuid);

        const sent = await api.post("friends/", { profile_uuid: them.uuid });
        expect(sent.status(), `sending answered ${sent.status()}: ${(await sent.text()).slice(0, 200)}`).toBeLessThan(300);

        const rejected = await secondaryApi.post(`friends/${me.uuid}/reject/`);
        expect(rejected.status(), `rejecting answered ${rejected.status()}: ${(await rejected.text()).slice(0, 200)}`).toBeLessThan(300);

        const outbound = await friends(api);
        const entry = entryFor(outbound, them.uuid);
        // Either gone, or present and plainly not accepted. What must not
        // happen is a rejected request reading as a friendship.
        if (entry) {
            expect(String(entry.status ?? "").toLowerCase(), `a rejected request reads as "${entry.status}"`).not.toContain("accept");
        }

        await unfriend(api, them.uuid);
    });

    ifSecondaryAccount()("a second request to the same person does not create a second friendship", async ({ api, secondaryApi }) => {
        // A retrying offline client, or an impatient user. Two rows for one
        // relationship is how a "friend" ends up listed twice and how one of
        // the two silently stops being honoured.
        const them = await whoami(secondaryApi);
        await unfriend(api, them.uuid);

        const first = await api.post("friends/", { profile_uuid: them.uuid });
        expect(first.status()).toBeLessThan(300);
        const second = await api.post("friends/", { profile_uuid: them.uuid });

        try {
            expect([200, 201, 400, 409], `a duplicate friend request answered ${second.status()}`).toContain(second.status());

            const outbound = await friends(api);
            const matches = outbound.results.filter((entry) => entry.profile?.uuid === them.uuid);
            expect(matches.length, `the same person appears ${matches.length} times in the friend feed`).toBeLessThanOrEqual(1);
        } finally {
            await unfriend(api, them.uuid);
        }
    });

    test("befriending yourself is refused", async ({ api }) => {
        const me = await whoami(api);

        // 404 in practice: the lookup that finds the other party excludes the
        // caller, so you are simply not a profile you can befriend. Accepted
        // alongside the more explicit refusals because the property that
        // matters is that it is refused with the standard envelope, not which
        // of several defensible codes it picks.
        const response = await api.post("friends/", { profile_uuid: me.uuid });
        expect([400, 403, 404, 409], `sending yourself a friend request answered ${response.status()}`).toContain(response.status());
        expect(await response.json()).toHaveProperty("error");
    });

    test("a profile uuid that belongs to nobody is refused with the standard envelope", async ({ api }) => {
        const response = await api.post("friends/", { profile_uuid: "00000000-0000-4000-8000-000000000000" });
        expect([400, 404], `an unknown profile uuid answered ${response.status()}`).toContain(response.status());
        expect(await response.json()).toHaveProperty("error");
    });

    test("a key without the social scope cannot send a request", async ({ api, restrictedApi }) => {
        const me = await whoami(api);
        const response = await restrictedApi.post("friends/", { profile_uuid: me.uuid });
        expect(response.status(), "a profile:read key sent a friend request").toBe(403);
    });
});
