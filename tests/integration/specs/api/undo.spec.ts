/**
 * Undo, end to end: delete something, find it, put it back.
 *
 * Worth a deployed test rather than a unit one because undo is the only feature
 * that spans every domain at once. The feed aggregates across each undoable
 * model, and each entry is filtered by whether the *calling credential* holds
 * the paired domain-read scope - so what a client sees depends on the
 * intersection of real rows, real scopes and a real restore path. A fixture
 * proves the aggregation function; only this proves that deleting a pin through
 * the API puts a restorable entry in the feed.
 *
 * It also pins the response envelope. `undo/` was published as a bare array
 * while returning `{entries, omitted}`, so a generated client iterated an
 * object (docs/PROBLEMS.md, 2026-08-24). The shape assertions below are what
 * notice if the two drift apart again.
 */

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";

/** The feed's documented shape. */
interface UndoFeed {
    entries: Array<{ uuid: string; model_label: string; object_repr: string; created: string; expires_at: string }>;
    omitted: string[];
}

test.describe("undo", () => {
    test("a deleted pin becomes a restorable entry", async ({ api }) => {
        const name = resourceName("undo round trip");
        const pin = await api.createPin({ name });

        const removed = await api.delete(`pins/${pin.slug}/`);
        expect(removed.ok(), `DELETE answered ${removed.status()}`).toBeTruthy();
        expect((await api.get(`pins/${pin.slug}/`)).status(), "the pin was still readable after being deleted").toBe(404);

        const feed = await api.json<UndoFeed>("get", "undo/");
        const entry = feed.entries.find((candidate) => candidate.object_repr?.includes(name));
        expect(
            entry,
            `the pin just deleted is not in the undo feed. Entries: ${feed.entries.map((e) => `${e.model_label}:${e.object_repr}`).join(", ") || "(none)"}`,
        ).toBeTruthy();

        // An entry a client cannot act on is not undo, it is a changelog.
        expect(entry?.uuid).toBeTruthy();
        expect(entry?.expires_at, "an undo entry with no expiry cannot be shown with a countdown").toBeTruthy();

        const restored = await api.post(`undo/${entry?.uuid}/restore/`);
        expect(restored.ok(), `restore answered ${restored.status()}: ${await restored.text()}`).toBeTruthy();

        // The whole point: the row is addressable again at the slug it had.
        const back = await api.get(`pins/${pin.slug}/`);
        expect(back.status(), "the pin did not come back after being restored").toBe(200);
        expect((await back.json()).name).toBe(name);
    });

    test("the feed is an envelope, not a bare array", async ({ api }) => {
        // The regression guard for the published-contract fix: `omitted` is a
        // client's only signal that its credential is missing a domain-read
        // scope, so flattening this response to an array would silently remove
        // the ability to prompt for re-authorization.
        const response = await api.get("undo/");
        expect(response.status()).toBe(200);

        const body = (await response.json()) as unknown;
        expect(Array.isArray(body), "undo/ returned a bare array; the documented shape is {entries, omitted}").toBeFalsy();

        const feed = body as UndoFeed;
        expect(Array.isArray(feed.entries), "undo/ has no `entries` array").toBeTruthy();
        expect(Array.isArray(feed.omitted), "undo/ has no `omitted` array").toBeTruthy();
    });

    test("restoring something that was never deleted is refused, not a crash", async ({ api }) => {
        const response = await api.post("undo/00000000-0000-4000-8000-000000000000/restore/");
        expect([404, 410], `expected a refusal, got ${response.status()}`).toContain(response.status());
        expect(await response.json()).toHaveProperty("error");
    });

    test("a key without the undo scope cannot read the feed", async ({ restrictedApi }) => {
        // The restricted key carries `profile:read` and nothing else. Scope
        // enforcement is declared per view and only a real request proves it is
        // wired up, as opposed to merely declared.
        const response = await restrictedApi.get("undo/");
        expect(response.status(), "a profile:read key was allowed to read the undo feed").toBe(403);
    });
});
