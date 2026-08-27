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

    ifSecondaryAccount()("a stranger's profile is indistinguishable from one that does not exist", async ({ api, secondaryApi }) => {
        // Not a 403. A profile you have no relationship with answers exactly
        // as a profile that was never created does, which is what stops a slug
        // being an oracle for who has an account here. Asserted as the two
        // answers being *identical* rather than against any particular
        // wording, for the same reason the pin suite does.
        const them = await whoami(secondaryApi);

        const stranger = await api.get(`profiles/${them.slug}/`);
        const nonexistent = await api.get("profiles/definitely-not-a-real-profile-91b2c/");

        expect(stranger.status(), `a stranger's profile answered ${stranger.status()}`).toBe(404);
        expect(nonexistent.status()).toBe(404);
        expect(
            await stranger.text(),
            "the answer for a real account you are not connected to differs from the one for an account that does not exist, which makes a slug an oracle for who has signed up",
        ).toBe(await nonexistent.text());
    });

    test("a profile slug that belongs to nobody is refused with the standard envelope", async ({ api }) => {
        const response = await api.get("profiles/definitely-not-a-real-profile-91b2c/");
        expect(response.status()).toBe(404);
        expect(await response.json()).toHaveProperty("error");
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
