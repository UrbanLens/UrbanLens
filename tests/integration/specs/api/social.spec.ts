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

/**
 * Every status the feed will filter by, from the schema's enum.
 *
 * Used when *proving* a relationship is gone, which is a different question
 * from finding a live one. An earlier version of the reset checked the settled
 * feed plus the two unsettled labels and called that clean - so a row sitting
 * in `Declined`, `Ignored`, `Blocked` or `Removed` was invisible to it, and the
 * next request in the file inherited whatever that row implied. That is the
 * shape of the remaining failure: the *first* test in this file is the one that
 * fails, which is exactly the test that meets the previous run's leftovers.
 */
const ALL_STATUSES = ["Pending", "Requested", "Accepted", "Declined", "Removed", "Muted", "Blocked", "Ignored"] as const;

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

    // Verified rather than assumed, because an unverified reset is how a stale
    // row becomes a mystery two tests later. `DELETE friends/{uuid}/` is
    // `remove_friend`, and if it declines to clear a row that is merely
    // *requested* - not yet a friendship - then the next `POST friends/` meets
    // an existing request and the test that follows fails somewhere else
    // entirely, with a message about the wrong thing.
    //
    // Reported from both seats: which side the surviving row belongs to is the
    // difference between "delete does not cover requests" and "delete is
    // one-directional", and the failure message should not make somebody guess.
    const survivors: string[] = [];
    for (const [label, client, otherUuid] of [
        ["requester", api, themUuid],
        ["recipient", secondaryApi, meUuid],
    ] as const) {
        for (const status of ALL_STATUSES) {
            // `Removed` is what DELETE leaves behind - the row is soft-deleted
            // rather than dropped, which is reasonable on its own (it keeps the
            // history). It is not leftover state in the sense this check is
            // looking for, so it is not reported here. What that surviving row
            // *does* to the next request is a defect in its own right, and has
            // its own test below rather than being folded into this helper.
            if (status === "Removed") {
                continue;
            }
            const entry = entryFor(await friends(client, status), otherUuid);
            if (entry) {
                survivors.push(`${label} still sees status="${entry.status}" direction="${entry.direction}" under ?status=${status}`);
            }
        }
    }

    expect(
        survivors,
        "a relationship survived being deleted from both sides, so every test below runs against leftover state:\n  " +
            survivors.join("\n  ") +
            "\nIf this is a status DELETE does not clear, that is the finding - `remove_friend` is documented as removing a friendship, " +
            "and a request or a block is not one.",
    ).toHaveLength(0);
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

        // No `message` here on purpose - the test below isolates that field,
        // because it is the one difference between this request and the ones
        // that accept cleanly.
        const requested = await api.post("friends/", { profile_uuid: them.uuid });
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

    ifSecondaryAccount()("re-adding somebody you removed produces a request they can accept", async ({ api, secondaryApi }) => {
        // The defect this file spent four runs narrowing down, reproduced
        // directly. `DELETE friends/{uuid}/` soft-deletes: the row survives with
        // status `Removed`, keeping whichever direction it originally had. A
        // later `POST friends/` finds that row and revives it *without*
        // re-orienting it, so the request is recorded as though the other person
        // had sent it - and the person it was actually sent to cannot accept it,
        // because from their side there is no incoming request.
        //
        // The user-visible shape: remove a friend, change your mind, add them
        // again, and the request sits there permanently unacceptable. Both
        // people see it, neither can act on it.
        //
        // Written from B's seat first so the surviving row belongs to B, which
        // is what makes A's later request the one that gets misfiled.
        const me = await whoami(api);
        const them = await whoami(secondaryApi);
        await resetFriendship(api, secondaryApi, me.uuid, them.uuid);

        // B befriends A, and then A removes the relationship.
        await secondaryApi.post("friends/", { profile_uuid: me.uuid });
        await api.post(`friends/${them.uuid}/accept/`);
        const removed = await api.delete(`friends/${them.uuid}/`);
        expect(removed.ok(), `removing answered ${removed.status()}`).toBeTruthy();

        try {
            // A now sends a fresh request in the *other* direction.
            const sent = await api.post("friends/", { profile_uuid: them.uuid });
            expect(sent.status(), `re-requesting answered ${sent.status()}: ${(await sent.text()).slice(0, 200)}`).toBeLessThan(300);

            const inbound = await findRelationship(secondaryApi, me.uuid);
            expect(inbound, "the re-sent request never reached the recipient").toBeTruthy();

            const accepted = await secondaryApi.post(`friends/${me.uuid}/accept/`);
            expect(
                accepted.status(),
                `the recipient could not accept a request sent to them (${accepted.status()}: ${(await accepted.text()).slice(0, 160)}). ` +
                    `They see it as status="${inbound?.status}" direction="${inbound?.direction}" - an incoming request labelled as one they sent, ` +
                    "which is the revived `Removed` row still carrying its original direction.",
            ).toBeLessThan(300);
        } finally {
            await resetFriendship(api, secondaryApi, me.uuid, them.uuid);
        }
    });

    ifSecondaryAccount()("a request carrying a note can still be accepted", async ({ api, secondaryApi }) => {
        // Isolating one field, because three consecutive live runs put the same
        // correlation on the table: requests sent *with* a `message` failed at
        // the accept step with "Friend request not found", while otherwise
        // identical requests sent without one accepted normally. The recipient
        // saw the request as `status="Requested" direction="outgoing"` - an
        // incoming request labelled as though they had sent it - which is
        // consistent either with the row being created the wrong way round or
        // with `direction` being computed from the row rather than the viewer.
        //
        // Written as its own test so the next run answers the question instead
        // of restating it: if this fails while the one above passes, `message`
        // is the cause and the difference is in `request_or_accept_friendship`'s
        // note-carrying path. If both pass, the earlier failures were leftover
        // state that `resetFriendship` now verifies away.
        const me = await whoami(api);
        const them = await whoami(secondaryApi);
        await resetFriendship(api, secondaryApi, me.uuid, them.uuid);

        const sent = await api.post("friends/", { profile_uuid: them.uuid, message: resourceName("a note with the request") });
        expect(sent.status(), `sending with a note answered ${sent.status()}: ${(await sent.text()).slice(0, 200)}`).toBeLessThan(300);

        try {
            const inbound = await findRelationship(secondaryApi, me.uuid);
            expect(inbound, "a request carrying a note never reached the recipient's feed").toBeTruthy();

            const accepted = await secondaryApi.post(`friends/${me.uuid}/accept/`);
            expect(
                accepted.status(),
                `a request carrying a note could not be accepted (${accepted.status()}: ${(await accepted.text()).slice(0, 160)}). ` +
                    `The recipient saw status="${inbound?.status}" direction="${inbound?.direction}". ` +
                    "The same exchange without a message accepts cleanly, so the note-carrying path creates a different row.",
            ).toBeLessThan(300);
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
