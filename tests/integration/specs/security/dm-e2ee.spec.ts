/**
 * GOALS: direct messages are end-to-end encrypted by default, as close to unconditionally enforced
 * as possible - there should be no way to turn it off.
 */

import type { APIResponse, Page } from "@playwright/test";

import { expect, ifSharingPair, test } from "../../lib/fixtures.js";
import { ensureFriends } from "../../lib/friendship.js";
import { freshMarker, sessionPost } from "../../lib/pin-share.js";
import { containsMarker, expectNotServerError } from "../../lib/security.js";

const messageRoutes = {
    thread: (slug: string) => `/dashboard/messages/${slug}/`,
    send: (slug: string) => `/dashboard/messages/${slug}/send/`,
    remove: (slug: string, id: number) => `/dashboard/messages/${slug}/delete/${id}/`,
};

/** An opaque, well-formed ciphertext triple the server cannot read. */
function opaqueCiphertext(marker: string): { ciphertext: string; nonce: string; key_version: string } {
    const bytes = Buffer.from(`${marker}-${crypto.randomUUID()}`);
    // XOR so the marker is not recoverable by base64-decoding the blob.
    const sealed = Buffer.from(bytes.map((byte) => byte ^ 0x5a));
    return { ciphertext: sealed.toString("base64"), nonce: Buffer.from(crypto.getRandomValues(new Uint8Array(24))).toString("base64"), key_version: "1" };
}

/** The id of the message whose bubble contains `needle` in the sender's re-rendered thread. */
function messageIdContaining(html: string, needle: string): number | null {
    const at = html.indexOf(needle);
    if (at === -1) {
        return null;
    }
    const ids = [...html.slice(0, at).matchAll(/id="dm-msg-(\d+)"/g)];
    const last = ids.at(-1)?.[1];
    return last ? Number(last) : null;
}

/** Tombstones a message this suite sent, so the pair's thread does not fill with test traffic. */
async function unsend(senderPage: Page, partnerSlug: string, sent: APIResponse, needle: string): Promise<void> {
    if (sent.status() !== 200) {
        return;
    }
    const id = messageIdContaining(await sent.text(), needle);
    if (id !== null) {
        await sessionPost(senderPage, messageRoutes.remove(partnerSlug, id), { scope: "everyone" }, messageRoutes.thread(partnerSlug));
    }
}

test.describe("direct messages are end-to-end encrypted, unconditionally", () => {
    ifSharingPair()("an opaque ciphertext is delivered as ciphertext, and malformed or mixed payloads are refused", async ({ sharerApi, shareeApi, sharerPage, shareePage }) => {
        // Preconditions for the expected-failure test below: the pair can message each other and the endpoint validates what it gets.
        const { a: sharer, b: sharee } = await ensureFriends(sharerApi, shareeApi);
        const marker = freshMarker("dmct");
        const sealed = opaqueCiphertext(marker);
        const warm = messageRoutes.thread(sharer.slug);

        const sent = await sessionPost(shareePage, messageRoutes.send(sharer.slug), { body: "", ...sealed }, warm);
        try {
            expect(sent.status(), `sending a well-formed ciphertext answered ${sent.status()}: ${(await sent.text()).slice(0, 200)}`).toBe(200);
            expect(await sent.text(), "the sender's thread does not show the message as encrypted").toContain(`data-e2ee-ct="${sealed.ciphertext}"`);

            const received = await sharerPage.request.get(messageRoutes.thread(sharee.slug));
            expect(received.status(), `the recipient's thread answered ${received.status()}`).toBe(200);
            const receivedHtml = await received.text();
            expect(receivedHtml, "the encrypted message did not reach the recipient").toContain(`data-e2ee-ct="${sealed.ciphertext}"`);
            expect(containsMarker(receivedHtml, marker), "the recipient's thread contains the plaintext behind a ciphertext the server should never see").toBeFalsy();
        } finally {
            await unsend(shareePage, sharer.slug, sent, `data-e2ee-ct="${sealed.ciphertext}"`);
        }

        const malformed = await sessionPost(shareePage, messageRoutes.send(sharer.slug), { body: "", ciphertext: "not base64!!", nonce: sealed.nonce, key_version: "1" }, warm);
        expect(malformed.status(), "a malformed ciphertext was accepted").toBe(400);
        expect(await malformed.text()).toContain("malformed");

        const mixedMarker = freshMarker("dmmix");
        const mixed = await sessionPost(shareePage, messageRoutes.send(sharer.slug), { body: mixedMarker, ...opaqueCiphertext(mixedMarker) }, warm);
        expect(mixed.status(), "a message carrying both plaintext and ciphertext was accepted").toBe(400);
        const afterMixed = await (await sharerPage.request.get(messageRoutes.thread(sharee.slug))).text();
        expect(containsMarker(afterMixed, mixedMarker), "a refused mixed message still reached the recipient").toBeFalsy();
    });

    ifSharingPair()("the server refuses a plaintext direct message", async ({ sharerApi, shareeApi, sharerPage, shareePage }) => {
        test.fail();
        test.info().annotations.push({
            type: "goals-conflict",
            description:
                '"Direct messages: end-to-end encrypted by default, as close to unconditionally enforced as possible - there should be no way to turn it off" vs ' +
                "controllers/direct_messages.py:324-350 + services/messaging/direct_messages.py:671-680 (a plain `body` with no ciphertext is accepted and stored; only plaintext+ciphertext together is refused) and models/direct_messages/model.py (plaintext body column)",
        });
        const { a: sharer, b: sharee } = await ensureFriends(sharerApi, shareeApi);
        const marker = freshMarker("dmplain");

        const sent = await sessionPost(shareePage, messageRoutes.send(sharer.slug), { body: `plaintext ${marker}` }, messageRoutes.thread(sharer.slug));
        try {
            await expectNotServerError(sent, "sending a plaintext direct message");
            expect(sent.status(), `a plaintext direct message answered ${sent.status()}; the server accepted a message it can read`).toBeGreaterThanOrEqual(400);
            expect(sent.status(), "a plaintext message was rate-limited rather than refused, which says nothing about encryption").not.toBe(429);

            const received = await (await sharerPage.request.get(messageRoutes.thread(sharee.slug))).text();
            expect(containsMarker(received, marker), "a plaintext direct message was delivered and is readable by the server").toBeFalsy();
        } finally {
            await unsend(shareePage, sharer.slug, sent, marker);
        }
    });
});
