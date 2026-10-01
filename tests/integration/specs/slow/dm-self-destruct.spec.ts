/**
 * Self-destructing direct messages (GOALS "Encryption"): once self-destructed, a message is gone from
 * the server, not just hidden. Only the "delete as soon as read" setting is exercised; whether an
 * unread message should ever time out is an open question and deliberately not asserted.
 *
 * Messaging is closed to API keys, so sends and probes go through the web views with a session.
 * A message's existence is probed with the reaction endpoint, which 404s once the row is gone.
 */

import type { APIResponse, Page } from "@playwright/test";

import { expect, ifSharingPair, test } from "../../lib/fixtures.js";
import { env, siteUrl } from "../../lib/env.js";
import { ensureFriends } from "../../lib/friendship.js";
import type { ApiClient } from "../../lib/api-client.js";

const GOALS_GONE =
    'GOALS: "once self-destructed (read or timed out), the message - encrypted blob included - is gone from the server, not just hidden"';

/** Minute past the hour `hard_delete_expired_direct_messages` runs (settings/base.py beat schedule). */
const SWEEP_MINUTE = 47;

function marker(): string {
    return `dm-${Math.random().toString(36).slice(2, 12)}`;
}

async function postForm(page: Page, path: string, form: Record<string, string>): Promise<APIResponse> {
    let token = (await page.context().cookies()).find((cookie) => cookie.name === "csrftoken")?.value;
    if (!token) {
        await page.request.get("/dashboard/messages/");
        token = (await page.context().cookies()).find((cookie) => cookie.name === "csrftoken")?.value;
    }
    expect(token, "no CSRF cookie, so a session POST would fail for the wrong reason").toBeTruthy();
    return page.request.post(path, { form, headers: { "X-CSRFToken": token ?? "", Origin: env.baseUrl, Referer: siteUrl(path) } });
}

/** Sends `body` from `sender` to `recipientSlug` and returns the new message's id. */
async function send(sender: Page, recipientSlug: string, body: string): Promise<number> {
    const response = await postForm(sender, `/dashboard/messages/${recipientSlug}/send/`, { body });
    expect(response.status(), `sending answered ${response.status()}: ${(await response.text()).slice(0, 200)}`).toBe(200);
    const bubble = (await response.text()).split('<li class="dm-bubble').find((chunk) => chunk.includes(body));
    const id = /data-message-id="(\d+)"/.exec(bubble ?? "")?.[1];
    expect(id, "the sent message is not in the thread the send returned").toBeTruthy();
    return Number(id);
}

/** 404 once the message row no longer exists for this participant. */
async function probe(page: Page, partnerSlug: string, id: number): Promise<number> {
    return (await postForm(page, `/dashboard/messages/${partnerSlug}/react/${id}/`, { emoji: "👍" })).status();
}

/** Whether conversation search returns a hit for `body`; the partial echoes the query, so hits are matched by markup. */
async function searchFinds(page: Page, partnerSlug: string, body: string): Promise<boolean> {
    const response = await page.request.get(`/dashboard/messages/${partnerSlug}/search/`, { params: { q: body } });
    expect(response.status()).toBe(200);
    return (await response.text()).includes('class="dm-search-result"');
}

async function thread(page: Page, partnerSlug: string): Promise<string> {
    const response = await page.request.get(`/dashboard/messages/${partnerSlug}/`);
    expect(response.status()).toBe(200);
    return response.text();
}

/** Sets the sender's retention for the test and restores it afterwards. */
async function withRetention(api: ApiClient, value: string, body: () => Promise<void>): Promise<void> {
    const before = await api.json<{ direct_message_delete_after: string }>("get", "settings/");
    const patched = await api.patch("settings/", { direct_message_delete_after: value });
    expect(patched.status(), `setting retention answered ${patched.status()}: ${(await patched.text()).slice(0, 200)}`).toBe(200);
    try {
        await body();
    } finally {
        await api.patch("settings/", { direct_message_delete_after: before.direct_message_delete_after });
    }
}

/** Waits until the sweep is not about to run, so an immediate check cannot pass by coinciding with it. */
async function awayFromSweep(): Promise<void> {
    const minute = new Date().getMinutes();
    if (minute >= SWEEP_MINUTE - 3 && minute <= SWEEP_MINUTE + 5) {
        await new Promise((resolve) => setTimeout(resolve, (SWEEP_MINUTE + 6 - minute) * 60_000));
    }
}

/** Milliseconds until the sweep after next could have finished. */
function untilSweepDone(): number {
    const now = new Date();
    const next = new Date(now);
    next.setMinutes(SWEEP_MINUTE, 0, 0);
    if (next <= now) next.setHours(next.getHours() + 1);
    return next.getTime() - now.getTime() + 10 * 60_000;
}

test.describe.serial("self-destructing direct messages", () => {
    ifSharingPair()("a message set to delete when read is shown once, then hidden from its recipient", async ({ sharerApi, shareeApi, sharerPage, shareePage }) => {
        const { a: sharer, b: sharee } = await ensureFriends(sharerApi, shareeApi);
        await withRetention(sharerApi, "when_read", async () => {
            const body = marker();
            const id = await send(sharerPage, sharee.slug, body);
            expect(await probe(sharerPage, sharee.slug, id), "an unread message does not exist for its sender").toBe(200);
            expect(await searchFinds(sharerPage, sharee.slug, body), "conversation search cannot find an unread message").toBe(true);

            expect(await thread(shareePage, sharer.slug), "the recipient never saw the message").toContain(body);
            expect(await thread(shareePage, sharer.slug), "the recipient still sees a message that was set to delete when read").not.toContain(body);
        });
    });

    ifSharingPair()("a read message set to delete when read is gone from the server, not hidden", async ({ sharerApi, shareeApi, sharerPage, shareePage }) => {
        test.fail();
        test.info().annotations.push({
            type: "goals-conflict",
            description: `${GOALS_GONE} vs models/direct_messages/model.py:118 hides it on read; queryset.py:87-98 hard-deletes only at the hourly :47 sweep`,
        });
        await awayFromSweep();
        const { a: sharer, b: sharee } = await ensureFriends(sharerApi, shareeApi);
        await withRetention(sharerApi, "when_read", async () => {
            const body = marker();
            const id = await send(sharerPage, sharee.slug, body);
            expect(await thread(shareePage, sharer.slug)).toContain(body);

            await expect.poll(async () => probe(sharerPage, sharee.slug, id), { message: "the sender can still address the message", timeout: 60_000 }).toBe(404);
            expect(await probe(shareePage, sharer.slug, id), "the recipient can still address the message").toBe(404);
            expect(await thread(sharerPage, sharee.slug), "the sender's thread still carries the message").not.toContain(body);
            expect(await searchFinds(sharerPage, sharee.slug, body), "conversation search still finds the message").toBe(false);
        });
    });

    ifSharingPair()("the hourly sweep deletes a read message set to delete when read", async ({ sharerApi, shareeApi, sharerPage, shareePage }) => {
        const { a: sharer, b: sharee } = await ensureFriends(sharerApi, shareeApi);
        await withRetention(sharerApi, "when_read", async () => {
            const body = marker();
            const id = await send(sharerPage, sharee.slug, body);
            expect(await probe(sharerPage, sharee.slug, id), "the probe cannot see a message that exists").toBe(200);
            expect(await thread(shareePage, sharer.slug)).toContain(body);

            await expect
                .poll(async () => probe(sharerPage, sharee.slug, id), { message: "the sweep never deleted the read message", timeout: untilSweepDone(), intervals: [60_000] })
                .toBe(404);
            expect(await probe(shareePage, sharer.slug, id)).toBe(404);
            expect(await thread(sharerPage, sharee.slug)).not.toContain(body);
        });
    });
});
