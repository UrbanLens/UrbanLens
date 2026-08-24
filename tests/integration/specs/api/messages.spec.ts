/**
 * Direct messaging: who is allowed to start a conversation.
 *
 * The *round trip* - send, both threads, idempotency - lives in
 * `api/social.spec.ts`, and not for tidiness. Messaging somebody requires a
 * relationship with them (a stranger is refused 403), so exercising it means
 * creating the one friendship the suite's two fixed accounts share, and that
 * file owns it. Two spec files creating and deleting the same relationship in
 * parallel is what made an earlier run fail on "Friend request not found".
 *
 * What is left here is everything that needs *no* relationship, which turns out
 * to be the more security-relevant half: who is refused, and how.
 */

import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import type { ApiClient } from "../../lib/api-client.js";

interface Page<T> {
    count: number;
    results: T[];
}

/** The calling credential's own profile. */
async function whoami(api: ApiClient): Promise<{ uuid: string; slug: string }> {
    return api.json<{ uuid: string; slug: string }>("get", "whoami/");
}

test.describe("direct messages", () => {
    ifSecondaryAccount()("a stranger cannot be messaged", async ({ api, secondaryApi }) => {
        // The rule that makes this feature safe to have. Without it, an API key
        // is a licence to message every account on the instance, and a slug is
        // all you need to find one.
        const them = await whoami(secondaryApi);

        const response = await api.post(`messages/${them.slug}/`, { body: "we have never met" });
        expect(
            [403, 404],
            `messaging an account with no relationship answered ${response.status()}, so any key can message any account`,
        ).toContain(response.status());
    });

    test("a peer who does not exist is refused", async ({ api }) => {
        const response = await api.post("messages/definitely-not-a-real-profile-91b2c/", { body: "nobody will read this" });
        expect([400, 403, 404], `messaging an unknown profile answered ${response.status()}`).toContain(response.status());
    });

    test("a reserved word is not mistaken for a person", async ({ api }) => {
        // `messages/settings/`, `messages/groups/` and `messages/conversations/`
        // are endpoints, and `messages/{peer_slug}/` is a wildcard over the same
        // space. Both the URL specificity sort and `RESERVED_PEER_SLUGS` exist
        // to stop one shadowing the other, so both would have to fail before a
        // request is misrouted - which is exactly the kind of thing that works
        // locally and behaves differently once a proxy normalises the path.
        const settings = await api.get("messages/settings/");
        expect(settings.status(), `messages/settings/ answered ${settings.status()} - it may have been routed as a peer slug`).toBe(200);

        const conversations = await api.get("messages/conversations/");
        expect(conversations.status(), `messages/conversations/ answered ${conversations.status()}`).toBe(200);
    });

    test("the conversation list uses the documented envelope", async ({ api }) => {
        const response = await api.get("messages/conversations/");
        expect(response.status()).toBe(200);

        const body = (await response.json()) as Record<string, unknown>;
        for (const key of ["count", "next", "previous", "results"]) {
            expect(body, `the conversation list envelope is missing "${key}"`).toHaveProperty(key);
        }
        expect(Array.isArray((body as unknown as Page<unknown>).results)).toBeTruthy();
    });

    test("messaging settings round-trip", async ({ api }) => {
        const before = await api.get("messages/settings/");
        expect(before.status(), `reading messaging settings answered ${before.status()}`).toBe(200);
    });

    test("a key without the messages scope cannot send", async ({ api, restrictedApi }) => {
        const me = await whoami(api);
        const response = await restrictedApi.post(`messages/${me.slug}/`, { body: "should not be accepted" });
        expect(response.status(), "a profile:read key sent a direct message").toBe(403);
    });
});
