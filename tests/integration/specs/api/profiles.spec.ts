/**
 * Profiles, read from your own seat and from somebody else's.
 *
 * The privacy surface. A profile serializer decides, per field, whether the
 * person asking is allowed to see it, and the decision depends on who they are
 * relative to the subject - themselves, a friend, a stranger. That is three
 * different renderings of one object, and a test that only ever asks as the
 * owner sees the one rendering that is always permissive.
 *
 * `is_self` and `friendship_status` are in the response precisely so a client
 * can tell which rendering it received, which makes them the honest thing to
 * assert on.
 */

import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import type { ApiClient } from "../../lib/api-client.js";

interface Profile {
    is_self?: boolean;
    friendship_status?: string | null;
    avatar_url?: string | null;
    bio?: string | null;
    contact?: unknown;
    area?: string | null;
}

/** The calling credential's own profile. */
async function whoami(api: ApiClient): Promise<{ uuid: string; slug: string }> {
    return api.json<{ uuid: string; slug: string }>("get", "whoami/");
}

test.describe("profiles", () => {
    test("your own profile identifies itself as yours", async ({ api }) => {
        const me = await whoami(api);
        const profile = await api.json<Profile>("get", `profiles/${me.slug}/`);

        // The flag a client keys "edit" affordances off. Getting it wrong shows
        // somebody an edit button for a profile they cannot change - or worse,
        // hides it on their own.
        expect(profile.is_self, "a profile fetched by its owner does not report is_self").toBe(true);
    });

    ifSecondaryAccount()("somebody else's profile is rendered as somebody else's", async ({ api, secondaryApi }) => {
        const them = await whoami(secondaryApi);

        const profile = await api.json<Profile>("get", `profiles/${them.slug}/`);
        expect(profile.is_self, "another account's profile came back flagged as your own").toBeFalsy();
        // Present, even when null - a client needs to distinguish "not friends"
        // from "the server did not say".
        expect(profile, "another account's profile carries no friendship_status, so a client cannot tell what it is looking at").toHaveProperty("friendship_status");
    });

    test("a profile slug that belongs to nobody is refused with the standard envelope", async ({ api }) => {
        const response = await api.get("profiles/definitely-not-a-real-profile-91b2c/");
        expect(response.status()).toBe(404);
        expect(await response.json()).toHaveProperty("error");
    });

    ifSecondaryAccount()("a private nickname for somebody else round-trips and is yours alone", async ({ api, secondaryApi }) => {
        const me = await whoami(api);
        const them = await whoami(secondaryApi);
        const nickname = resourceName("nickname");

        const set = await api.put(`profiles/${them.slug}/nickname/`, { nickname });
        expect(set.status(), `setting a nickname answered ${set.status()}: ${(await set.text()).slice(0, 200)}`).toBeLessThan(300);

        try {
            const seen = await api.json<Record<string, unknown>>("get", `profiles/${them.slug}/`);
            expect(JSON.stringify(seen), "the nickname just set is not visible to the person who set it").toContain(nickname);

            // The half that matters: a nickname is a private annotation. If the
            // subject can see what you call them, it is not one.
            const theirOwnView = await secondaryApi.json<Record<string, unknown>>("get", `profiles/${them.slug}/`);
            expect(
                JSON.stringify(theirOwnView),
                "the nickname one account set for another is visible to the person it describes",
            ).not.toContain(nickname);

            // And it must not leak to the person's view of *you* either.
            const theirViewOfMe = await secondaryApi.json<Record<string, unknown>>("get", `profiles/${me.slug}/`);
            expect(JSON.stringify(theirViewOfMe), "a private nickname leaked into the other account's view").not.toContain(nickname);
        } finally {
            await api.delete(`profiles/${them.slug}/nickname/`);
        }
    });

    ifSecondaryAccount()("a private note about somebody is not visible to them", async ({ api, secondaryApi }) => {
        const them = await whoami(secondaryApi);
        const content = resourceName("private note");

        const created = await api.post(`profiles/${them.slug}/notes/`, { content });
        expect(created.status(), `creating a note answered ${created.status()}: ${(await created.text()).slice(0, 200)}`).toBeLessThan(300);

        const mine = await api.get(`profiles/${them.slug}/notes/`);
        expect(mine.status()).toBe(200);
        expect(await mine.text(), "a note just written is not in its author's list").toContain(content);

        // The subject asking for notes about themselves must not receive the
        // ones other people wrote.
        const theirs = await secondaryApi.get(`profiles/${them.slug}/notes/`);
        if (theirs.status() === 200) {
            expect(await theirs.text(), "a private note about somebody is readable by that person").not.toContain(content);
        }
    });

    ifSecondaryAccount()("a trust rating round-trips", async ({ api, secondaryApi }) => {
        const them = await whoami(secondaryApi);

        const rated = await api.put(`profiles/${them.slug}/trust/`, { rating: 1 });
        expect(rated.status(), `setting trust answered ${rated.status()}: ${(await rated.text()).slice(0, 200)}`).toBeLessThan(300);

        const removed = await api.delete(`profiles/${them.slug}/trust/`);
        expect(removed.ok(), `clearing trust answered ${removed.status()}`).toBeTruthy();
    });

    test("your own preferences round-trip", async ({ api }) => {
        const me = await whoami(api);
        const area = resourceName("area");

        const patched = await api.patch(`profiles/${me.slug}/`, { area });
        expect(patched.status(), `PATCH answered ${patched.status()}: ${(await patched.text()).slice(0, 200)}`).toBe(200);

        const after = await api.json<Profile>("get", `profiles/${me.slug}/`);
        expect(after.area, "an edited profile field did not persist").toBe(area);
    });

    ifSecondaryAccount()("one account cannot edit another's profile", async ({ api, secondaryApi }) => {
        const them = await whoami(secondaryApi);
        const response = await api.patch(`profiles/${them.slug}/`, { bio: "written by somebody else" });

        expect([403, 404], `editing another account's profile answered ${response.status()}`).toContain(response.status());
    });
});
