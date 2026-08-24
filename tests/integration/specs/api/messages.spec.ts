/**
 * An API key must not be a way into anybody's conversations.
 *
 * This file does not test messaging. It tests that messaging is *unreachable*,
 * which is the whole of what an API key is entitled to know about it.
 *
 * `permissions.OAUTH2_ONLY_SCOPES` restricts `messages:read`/`messages:write`
 * to user-consented OAuth2 tokens, so a PAT-style key is refused across the
 * entire surface **even when its own `scopes` list names those scopes** - and
 * the suite's provisioned keys do name them, which makes this account exactly
 * the adversary the rule exists for. The reasoning in `views_messaging` is
 * worth repeating: a bearer key that ends up in a CI config or a screenshot
 * must not read somebody's direct messages.
 *
 * That is a property with no natural place to fail loudly. A scope added to the
 * wrong list, or a new messaging view that forgets the restriction, opens the
 * whole surface silently - nothing breaks, a door just opens. So every endpoint
 * is asked, rather than a representative one: the risk is precisely that
 * *one* of them is different.
 *
 * The consequence for coverage is recorded in docs/INTEGRATION_TESTS.md: real
 * messaging behaviour cannot be exercised by this suite at all, because doing
 * so needs an OAuth2 authorization-code flow the harness has no way to drive.
 */

import { expect, test } from "../../lib/fixtures.js";
import type { ApiClient } from "../../lib/api-client.js";

/** The calling credential's own profile. */
async function whoami(api: ApiClient): Promise<{ uuid: string; slug: string }> {
    return api.json<{ uuid: string; slug: string }>("get", "whoami/");
}

/**
 * Every messaging endpoint reachable without knowing another account.
 *
 * `{self}` is substituted with the caller's own slug - messaging yourself is
 * still messaging, and if the restriction were missing this is the request that
 * would prove it without needing a second account to point at.
 */
const READ_ENDPOINTS = ["messages/settings/", "messages/conversations/", "messages/groups/", "messages/{self}/"] as const;

const WRITE_ENDPOINTS = [
    { path: "messages/{self}/", body: { body: "an API key should not be able to send this" } },
    { path: "messages/groups/", body: { name: "an API key should not be able to create this" } },
    { path: "messages/{self}/read/", body: {} },
] as const;

test.describe("messaging is closed to API keys", () => {
    test("every messaging read endpoint refuses an API key", async ({ api }) => {
        const me = await whoami(api);

        const reachable: string[] = [];
        for (const endpoint of READ_ENDPOINTS) {
            const path = endpoint.replace("{self}", me.slug);
            const response = await api.get(path);
            if (response.status() !== 403) {
                reachable.push(`GET ${path} -> ${response.status()}`);
            }
        }

        expect(
            reachable,
            `an API key reached messaging endpoints that OAUTH2_ONLY_SCOPES is supposed to close to it:\n  ${reachable.join("\n  ")}`,
        ).toHaveLength(0);
    });

    test("every messaging write endpoint refuses an API key", async ({ api }) => {
        const me = await whoami(api);

        const reachable: string[] = [];
        for (const endpoint of WRITE_ENDPOINTS) {
            const path = endpoint.path.replace("{self}", me.slug);
            const response = await api.post(path, endpoint.body);
            if (response.status() !== 403) {
                reachable.push(`POST ${path} -> ${response.status()}: ${(await response.text()).slice(0, 120)}`);
            }
        }

        expect(reachable, `an API key wrote through messaging endpoints it should not reach:\n  ${reachable.join("\n  ")}`).toHaveLength(0);
    });

    test("the refusal does not depend on the peer existing", async ({ api }) => {
        // A 404 here instead of a 403 would leak whether a slug belongs to
        // somebody, to a caller who is not allowed into messaging at all. The
        // authorization check has to come first.
        const unknown = await api.post("messages/definitely-not-a-real-profile-91b2c/", { body: "nobody will read this" });
        expect(
            unknown.status(),
            `messaging an unknown profile answered ${unknown.status()} - a caller refused messaging entirely should not be able to tell a real slug from a fake one`,
        ).toBe(403);
    });

    test("a restricted key is refused too", async ({ api, restrictedApi }) => {
        // The same answer from a key that does not even name the scopes, so a
        // regression cannot be masked by the restricted key being refused for a
        // different reason than the full one.
        const me = await whoami(api);
        const response = await restrictedApi.get(`messages/${me.slug}/`);
        expect(response.status(), "a profile:read key reached a message thread").toBe(403);
    });
});
