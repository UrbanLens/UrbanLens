/**
 * Direct messages, sent by one account and read by the other.
 *
 * The second domain in the suite that needs two accounts, and the one where
 * "both sides see it" is the entire feature rather than a nicety. A message
 * that reaches the sender's own thread and nobody else's is a plausible
 * implementation of a broken product, and it is indistinguishable from a
 * working one in a test that only ever asks the sender.
 *
 * Sent as plaintext `body` on purpose. The endpoint also accepts
 * `ciphertext`/`nonce`/`key_version` for end-to-end-encrypted conversations,
 * but exercising that would mean reimplementing the browser's libsodium in the
 * test - and the server, correctly, cannot tell whether what it stored decrypts.
 * The transport is what this can check; the crypto is checked by the
 * server-side interop tests in the pytest suite.
 */

import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import type { ApiClient } from "../../lib/api-client.js";

interface Page<T> {
    count: number;
    results: T[];
}

interface Message {
    id?: number;
    uuid?: string;
    body?: string;
    client_uuid?: string;
}

/** The calling credential's own profile. */
async function whoami(api: ApiClient): Promise<{ uuid: string; slug: string }> {
    return api.json<{ uuid: string; slug: string }>("get", "whoami/");
}

// Serial for the same reason as the friendship specs: every test here writes to
// the one conversation between the suite's two fixed accounts.
test.describe.serial("direct messages", () => {
    ifSecondaryAccount()("a message sent by one account is readable by the other", async ({ api, secondaryApi }) => {
        const me = await whoami(api);
        const them = await whoami(secondaryApi);
        const body = resourceName("hello from the suite");

        const sent = await api.post(`messages/${them.slug}/`, { body });
        expect(sent.status(), `sending answered ${sent.status()}: ${(await sent.text()).slice(0, 250)}`).toBeLessThan(300);

        // The recipient's copy of the same thread, addressed by *their* peer -
        // which is the sender. Getting this wrong is how a thread ends up
        // one-directional.
        const inbox = await secondaryApi.json<Page<Message>>("get", `messages/${me.slug}/`);
        expect(
            inbox.results.some((message) => message.body === body),
            `the recipient's thread does not contain the message. Last few: ${JSON.stringify(inbox.results.slice(0, 3)).slice(0, 300)}`,
        ).toBeTruthy();

        // And the sender keeps their own copy, or the thread reads as empty to
        // the person who just typed into it.
        const outbox = await api.json<Page<Message>>("get", `messages/${them.slug}/`);
        expect(outbox.results.some((message) => message.body === body), "the sender's own thread does not contain what they sent").toBeTruthy();
    });

    ifSecondaryAccount()("the conversation appears in the recipient's conversation list", async ({ api, secondaryApi }) => {
        const me = await whoami(api);
        const them = await whoami(secondaryApi);
        await api.post(`messages/${them.slug}/`, { body: resourceName("conversation starter") });

        const conversations = await secondaryApi.json<Page<Record<string, unknown>>>("get", "messages/conversations/");
        expect(
            JSON.stringify(conversations.results),
            `the sender does not appear in the recipient's conversation list, so the thread exists but is unreachable from the inbox`,
        ).toContain(me.slug);
    });

    ifSecondaryAccount()("a resend with the same client_uuid does not duplicate the message", async ({ api, secondaryApi }) => {
        // The same offline-outbox problem pins have: a client stamps an id at
        // compose time and retries until acknowledged. Without idempotency a
        // flaky connection sends the message twice.
        const them = await whoami(secondaryApi);
        const clientUuid = crypto.randomUUID();
        const body = resourceName("sent twice");

        const first = await api.post(`messages/${them.slug}/`, { body, client_uuid: clientUuid });
        expect(first.status()).toBeLessThan(300);
        const second = await api.post(`messages/${them.slug}/`, { body, client_uuid: clientUuid });
        expect(second.status(), `the retry answered ${second.status()}`).toBeLessThan(300);

        const thread = await api.json<Page<Message>>("get", `messages/${them.slug}/`);
        const copies = thread.results.filter((message) => message.body === body);
        expect(copies.length, `a retried send produced ${copies.length} copies of the same message`).toBe(1);
    });

    ifSecondaryAccount()("a thread can be muted and unmuted", async ({ api, secondaryApi }) => {
        const them = await whoami(secondaryApi);

        const muted = await api.put(`messages/${them.slug}/mute/`, {});
        expect(muted.status(), `muting answered ${muted.status()}: ${(await muted.text()).slice(0, 200)}`).toBeLessThan(300);

        const state = await api.get(`messages/${them.slug}/mute/`);
        expect(state.status(), `reading the mute state answered ${state.status()}`).toBe(200);

        const unmuted = await api.delete(`messages/${them.slug}/mute/`);
        expect(unmuted.ok(), `unmuting answered ${unmuted.status()}`).toBeTruthy();
    });

    ifSecondaryAccount()("marking a thread read is accepted", async ({ api, secondaryApi }) => {
        const me = await whoami(api);
        await api.post(`messages/${(await whoami(secondaryApi)).slug}/`, { body: resourceName("to be read") });

        const read = await secondaryApi.post(`messages/${me.slug}/read/`);
        expect(read.status(), `marking read answered ${read.status()}: ${(await read.text()).slice(0, 200)}`).toBeLessThan(300);
    });

    test("a thread with somebody who does not exist is refused", async ({ api }) => {
        const response = await api.post("messages/definitely-not-a-real-profile-91b2c/", { body: "nobody will read this" });
        expect([400, 403, 404], `messaging an unknown profile answered ${response.status()}`).toContain(response.status());
    });

    test("a key without the messages scope cannot send", async ({ api, restrictedApi }) => {
        const me = await whoami(api);
        const response = await restrictedApi.post(`messages/${me.slug}/`, { body: "should not be accepted" });
        expect(response.status(), "a profile:read key sent a direct message").toBe(403);
    });
});
