/**
 * The notification API a native client polls.
 *
 * Distinct from `ui/notifications.spec.ts`, which asks whether a notification
 * reaches a *browser*. This asks whether the same thing reaches a client with
 * no browser at all - the mobile app - and the two go wrong independently: the
 * shell's HTMX fragment and this endpoint read the same rows through different
 * code.
 *
 * The unread count is the part worth being careful about. It is displayed as a
 * badge, so an off-by-one is visible on every screen, and it is derived rather
 * than stored - which means "mark this read" and "how many are unread" can
 * disagree without either being obviously broken on its own.
 */

import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import type { ApiClient } from "../../lib/api-client.js";

interface NotificationFeed {
    next_cursor: string | null;
    unread_count: number;
    results: Array<{ uuid?: string; read?: boolean; is_read?: boolean }>;
}

/** The calling credential's own profile. */
async function whoami(api: ApiClient): Promise<{ uuid: string; slug: string }> {
    return api.json<{ uuid: string; slug: string }>("get", "whoami/");
}

/** Whether an entry reads as already seen, under either spelling. */
function isRead(entry: { read?: boolean; is_read?: boolean }): boolean {
    return Boolean(entry.read ?? entry.is_read);
}

test.describe.serial("notifications", () => {
    test("the feed carries a cursor and an unread count", async ({ api }) => {
        const feed = await api.json<NotificationFeed>("get", "notifications/");

        expect(Array.isArray(feed.results), "the notification feed has no results array").toBeTruthy();
        // Both are load-bearing for a client: without the count it has to walk
        // the whole feed to render a badge, and without the cursor it cannot
        // page at all.
        expect(feed, "the feed carries no unread_count, so a badge costs a full walk").toHaveProperty("unread_count");
        expect(feed, "the feed carries no next_cursor").toHaveProperty("next_cursor");
    });

    test("the unread count endpoint agrees with the feed", async ({ api }) => {
        // Two endpoints, one number. They are read by different surfaces - the
        // badge polls the cheap one - so a disagreement shows up as a badge
        // that never clears.
        const feed = await api.json<NotificationFeed>("get", "notifications/");
        const counter = await api.json<Record<string, unknown>>("get", "notifications/unread-count/");

        const reported = Number(counter.unread_count ?? counter.count ?? counter.unread);
        expect(Number.isFinite(reported), `the unread-count endpoint returned no recognisable number: ${JSON.stringify(counter)}`).toBeTruthy();
        expect(reported, `notifications/ says ${feed.unread_count} unread and notifications/unread-count/ says ${reported}`).toBe(feed.unread_count);
    });

    test("marking one notification read reduces the unread count", async ({ api }) => {
        // Deliberately does *not* manufacture a notification. Every cross-account
        // action that would produce one - a friend request, a message - writes to
        // state shared with `api/social.spec.ts`, and files run in parallel, so
        // two specs racing over the one friendship between the suite's two fixed
        // accounts produced failures that read as endpoint faults. The
        // *arrival* of a notification is asserted in social.spec.ts, which owns
        // that relationship and is serial; what is left here is the mechanics,
        // which need any unread notification rather than a particular one.
        const feed = await api.json<NotificationFeed>("get", "notifications/");
        const unread = feed.results.find((entry) => !isRead(entry) && entry.uuid);
        test.skip(!unread, "This account has nothing unread, so there is no read transition to observe.");

        const marked = await api.post(`notifications/${unread?.uuid}/`, {});
        expect(marked.status(), `marking one read answered ${marked.status()}: ${(await marked.text()).slice(0, 200)}`).toBeLessThan(300);

        const after = await api.json<NotificationFeed>("get", "notifications/");
        const stillUnread = after.results.find((entry) => entry.uuid === unread?.uuid && !isRead(entry));
        expect(stillUnread, "a notification marked read still reads as unread").toBeFalsy();
        expect(after.unread_count, "marking one notification read did not reduce the unread count").toBeLessThan(feed.unread_count);
    });

    test("marking everything read leaves nothing unread", async ({ api }) => {
        const cleared = await api.post("notifications/read-all/", {});
        expect(cleared.status(), `read-all answered ${cleared.status()}: ${(await cleared.text()).slice(0, 200)}`).toBeLessThan(300);

        const feed = await api.json<NotificationFeed>("get", "notifications/");
        expect(feed.unread_count, `after read-all the unread count is ${feed.unread_count}`).toBe(0);
        expect(feed.results.filter((entry) => !isRead(entry)), "after read-all some entries still read as unread").toHaveLength(0);
    });

    test("marking a notification that does not exist is refused rather than a crash", async ({ api }) => {
        const response = await api.post("notifications/00000000-0000-4000-8000-000000000000/", {});
        expect([400, 404], `an unknown notification uuid answered ${response.status()}`).toContain(response.status());
    });

    test("a key without the notifications scope cannot read the feed", async ({ restrictedApi }) => {
        const response = await restrictedApi.get("notifications/");
        expect(response.status(), "a profile:read key read the notification feed").toBe(403);
    });
});
