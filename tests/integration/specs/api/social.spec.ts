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
import { resourceName } from "../../lib/env.js";
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
 * The statuses an unsettled request can be in, from either seat.
 *
 * The schema's enum carries both `Pending` and `Requested`, and the pair is the
 * point: one row is described differently depending on which end is asking, so
 * the sender's outstanding request and the recipient's incoming one are not the
 * same string. Searching both is what keeps this about the friendship rather
 * than about which label the API picked for which direction.
 *
 * Capitalisation matters too - `?status=pending` is a different string and
 * quietly returns the unfiltered feed rather than an error.
 */
const UNSETTLED = ["Pending", "Requested"] as const;

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
 * The default feed does not include an unsettled request - one is not yet a
 * friendship - so anything looking for one has to ask for it.
 */
async function friends(api: ApiClient, status?: string): Promise<FriendFeed> {
    return api.json<FriendFeed>("get", "friends/", status ? { status } : undefined);
}

/** Looks for a relationship with `profileUuid` in the settled feed and both unsettled ones. */
async function findRelationship(api: ApiClient, profileUuid: string): Promise<FriendEntry | undefined> {
    const found = entryFor(await friends(api), profileUuid);
    if (found) {
        return found;
    }
    for (const status of UNSETTLED) {
        const entry = entryFor(await friends(api, status), profileUuid);
        if (entry) {
            return entry;
        }
    }
    return undefined;
}

/**
 * Returns the pair to having no relationship at all, from both sides.
 *
 * Both sides, because a row left in any state - declined, blocked, still
 * pending - makes the next `POST friends/` answer 400 "Could not send that
 * friend request", which reads as a broken endpoint rather than as leftover
 * state from the previous test.
 */
async function resetFriendship(api: ApiClient, secondaryApi: ApiClient, meUuid: string, themUuid: string): Promise<void> {
    await api.delete(`friends/${themUuid}/`);
    await secondaryApi.delete(`friends/${meUuid}/`);
}

// Serial, because every test in this file manipulates the *one* relationship
// between the suite's two fixed accounts. Run in parallel they take it from
// each other, and the failures that produces ("Could not send that friend
// request") look like endpoint faults rather than like two tests sharing a row.
test.describe.serial("friendships", () => {
    ifSecondaryAccount()("a request is visible to the recipient, and acceptance to both", async ({ api, secondaryApi }) => {
        const me = await whoami(api);
        const them = await whoami(secondaryApi);
        await resetFriendship(api, secondaryApi, me.uuid, them.uuid);

        const requested = await api.post("friends/", { profile_uuid: them.uuid, message: "Sent by the UrbanLens integration suite." });
        expect(requested.status(), `sending a friend request answered ${requested.status()}: ${(await requested.text()).slice(0, 200)}`).toBeLessThan(300);

        try {
            // The recipient's view. A request that only the sender can see is
            // the failure mode worth catching: it looks sent and never arrives.
            const inbound = await findRelationship(secondaryApi, me.uuid);
            expect(inbound, "the recipient cannot see the request in any feed - settled or unsettled - so it was sent to nobody").toBeTruthy();

            // `request_or_accept_friendship` accepts outright when the other
            // party already had a request pending in reverse - two people who
            // clearly want to be friends should not need a second round trip.
            // So the request may already be settled here, and calling accept on
            // a friendship with nothing pending is a 404 rather than a no-op.
            // The outcome is what this test is about, not which of the two
            // routes reached it.
            const alreadyAccepted = String(inbound?.status ?? "").toLowerCase().includes("accept");
            if (!alreadyAccepted) {
                const accepted = await secondaryApi.post(`friends/${me.uuid}/accept/`);
                expect(
                    accepted.status(),
                    `accepting answered ${accepted.status()}: ${(await accepted.text()).slice(0, 200)}. ` +
                        `Immediately before, the recipient saw status="${inbound?.status}" direction="${inbound?.direction}".`,
                ).toBeLessThan(300);
            }

            // And now from the requester's seat, which is the half that a
            // one-sided implementation gets wrong.
            const outbound = await friends(api);
            const entry = entryFor(outbound, them.uuid);
            expect(entry, "after acceptance the requester's feed no longer lists the friend").toBeTruthy();
            expect(String(entry?.status ?? "").toLowerCase(), `the friendship reads as "${entry?.status}" to the requester after being accepted`).toContain("accept");
        } finally {
            await resetFriendship(api, secondaryApi, me.uuid, them.uuid);
        }
    });

    ifSecondaryAccount()("a friend request notifies the person it was sent to", async ({ api, secondaryApi }) => {
        // Lives here rather than in the notifications spec because it needs the
        // friendship, and this file is the one that owns it. Files run in
        // parallel, so two specs both creating and deleting the single
        // relationship between the suite's two fixed accounts race, and the
        // failures that produces look like endpoint faults.
        const me = await whoami(api);
        const them = await whoami(secondaryApi);
        await resetFriendship(api, secondaryApi, me.uuid, them.uuid);

        const before = await api.json<{ unread_count: number }>("get", "notifications/");
        const sent = await secondaryApi.post("friends/", { profile_uuid: me.uuid, message: "Sent by the integration suite." });
        expect(sent.status(), `sending answered ${sent.status()}: ${(await sent.text()).slice(0, 200)}`).toBeLessThan(300);

        try {
            // The notification is written in the request that created the
            // friendship, but a deployment may route it through the channel
            // layer or a task - so this polls rather than reading once and
            // calling the timing of this machine a result.
            await expect
                .poll(async () => (await api.json<{ unread_count: number }>("get", "notifications/")).unread_count, {
                    message: `a friend request from the other account never raised this account's unread count (was ${before.unread_count})`,
                    timeout: 20_000,
                })
                .toBeGreaterThan(before.unread_count);

            const feed = await api.json<{ results: Array<{ notification_type?: string; message?: string }> }>("get", "notifications/");
            expect(
                JSON.stringify(feed.results.slice(0, 5)),
                "the unread count went up but no friend-request notification is in the feed",
            ).toContain("friend");
        } finally {
            await resetFriendship(api, secondaryApi, me.uuid, them.uuid);
        }
    });

    ifSecondaryAccount()("a rejected request does not become a friendship", async ({ api, secondaryApi }) => {
        const me = await whoami(api);
        const them = await whoami(secondaryApi);
        await resetFriendship(api, secondaryApi, me.uuid, them.uuid);

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

        await resetFriendship(api, secondaryApi, me.uuid, them.uuid);
    });

    ifSecondaryAccount()("a second request to the same person does not create a second friendship", async ({ api, secondaryApi }) => {
        // A retrying offline client, or an impatient user. Two rows for one
        // relationship is how a "friend" ends up listed twice and how one of
        // the two silently stops being honoured.
        const me = await whoami(api);
        const them = await whoami(secondaryApi);
        await resetFriendship(api, secondaryApi, me.uuid, them.uuid);

        const first = await api.post("friends/", { profile_uuid: them.uuid });
        expect(first.status()).toBeLessThan(300);
        const second = await api.post("friends/", { profile_uuid: them.uuid });

        try {
            expect([200, 201, 400, 409], `a duplicate friend request answered ${second.status()}`).toContain(second.status());

            const outbound = await friends(api);
            const matches = outbound.results.filter((entry) => entry.profile?.uuid === them.uuid);
            expect(matches.length, `the same person appears ${matches.length} times in the friend feed`).toBeLessThanOrEqual(1);
        } finally {
            await resetFriendship(api, secondaryApi, me.uuid, them.uuid);
        }
    });

    ifSecondaryAccount()("becoming friends makes the profile visible, and private annotations stay private", async ({ api, secondaryApi }) => {
        // These annotations live here rather than in `profiles.spec.ts` for two
        // reasons. They need a *visible* profile - a stranger's answers 404, by
        // design - and creating the friendship that grants visibility writes to
        // the one relationship this file owns. Somewhere else doing the same
        // thing in parallel is what made an earlier run fail on "Friend request
        // not found".
        const me = await whoami(api);
        const them = await whoami(secondaryApi);
        await resetFriendship(api, secondaryApi, me.uuid, them.uuid);

        await api.post("friends/", { profile_uuid: them.uuid });
        const accepted = await secondaryApi.post(`friends/${me.uuid}/accept/`);
        expect(accepted.status(), `accepting answered ${accepted.status()}: ${(await accepted.text()).slice(0, 200)}`).toBeLessThan(300);

        try {
            const profile = await api.get(`profiles/${them.slug}/`);
            expect(profile.status(), "a friend's profile is still not visible after the friendship was accepted").toBe(200);
            const body = (await profile.json()) as { is_self?: boolean };
            expect(body.is_self, "a friend's profile came back flagged as your own").toBeFalsy();

            const nickname = resourceName("nickname");
            const set = await api.put(`profiles/${them.slug}/nickname/`, { nickname });
            expect(set.status(), `setting a nickname answered ${set.status()}: ${(await set.text()).slice(0, 200)}`).toBeLessThan(300);

            try {
                const mine = await api.json<Record<string, unknown>>("get", `profiles/${them.slug}/`);
                expect(JSON.stringify(mine), "the nickname just set is not visible to the person who set it").toContain(nickname);

                // The half that matters. A nickname is a private annotation; if
                // the subject can read what you call them, it is not one.
                const theirOwnView = await secondaryApi.json<Record<string, unknown>>("get", `profiles/${them.slug}/`);
                expect(JSON.stringify(theirOwnView), "the nickname one account set for another is visible to the person it describes").not.toContain(nickname);
            } finally {
                await api.delete(`profiles/${them.slug}/nickname/`);
            }

            const content = resourceName("private note");
            const noted = await api.post(`profiles/${them.slug}/notes/`, { content });
            expect(noted.status(), `creating a note answered ${noted.status()}: ${(await noted.text()).slice(0, 200)}`).toBeLessThan(300);

            const theirNotes = await secondaryApi.get(`profiles/${them.slug}/notes/`);
            if (theirNotes.status() === 200) {
                expect(await theirNotes.text(), "a private note about somebody is readable by that person").not.toContain(content);
            }
        } finally {
            await resetFriendship(api, secondaryApi, me.uuid, them.uuid);
        }
    });

    ifSecondaryAccount()("friends can exchange direct messages", async ({ api, secondaryApi }) => {
        // Here rather than in `api/messages.spec.ts` because messaging somebody
        // requires a relationship with them - a stranger is refused - and this
        // file owns the one relationship the two fixed accounts share.
        const me = await whoami(api);
        const them = await whoami(secondaryApi);
        await resetFriendship(api, secondaryApi, me.uuid, them.uuid);
        await api.post("friends/", { profile_uuid: them.uuid });
        await secondaryApi.post(`friends/${me.uuid}/accept/`);

        try {
            const body = resourceName("hello from the suite");
            const sent = await api.post(`messages/${them.slug}/`, { body });
            expect(sent.status(), `sending answered ${sent.status()}: ${(await sent.text()).slice(0, 250)}`).toBeLessThan(300);

            // The recipient's copy of the same thread, addressed by *their*
            // peer - which is the sender. Getting this wrong is how a thread
            // ends up one-directional: it looks sent and arrives nowhere.
            const inbox = await secondaryApi.json<{ results: Array<{ body?: string }> }>("get", `messages/${me.slug}/`);
            expect(
                inbox.results.some((message) => message.body === body),
                `the recipient's thread does not contain the message. Last few: ${JSON.stringify(inbox.results.slice(0, 3)).slice(0, 250)}`,
            ).toBeTruthy();

            // And the sender keeps their own copy, or the thread reads as empty
            // to the person who just typed into it.
            const outbox = await api.json<{ results: Array<{ body?: string }> }>("get", `messages/${them.slug}/`);
            expect(outbox.results.some((message) => message.body === body), "the sender's own thread does not contain what they sent").toBeTruthy();

            // The offline-outbox problem pins have too: a client stamps an id
            // at compose time and retries until acknowledged.
            const clientUuid = crypto.randomUUID();
            const retried = resourceName("sent twice");
            await api.post(`messages/${them.slug}/`, { body: retried, client_uuid: clientUuid });
            await api.post(`messages/${them.slug}/`, { body: retried, client_uuid: clientUuid });

            const thread = await api.json<{ results: Array<{ body?: string }> }>("get", `messages/${them.slug}/`);
            const copies = thread.results.filter((message) => message.body === retried);
            expect(copies.length, `a retried send with one client_uuid produced ${copies.length} copies`).toBe(1);
        } finally {
            await resetFriendship(api, secondaryApi, me.uuid, them.uuid);
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
