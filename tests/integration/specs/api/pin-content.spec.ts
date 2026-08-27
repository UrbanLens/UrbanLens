/**
 * The things that hang off a pin: links, notes, comments.
 *
 * Three small collections that share one shape - a child row addressed under
 * its parent's slug - and that is exactly why they are worth testing together.
 * The interesting question is not whether a note can be created; it is whether
 * each of these consistently refuses a *parent* the caller does not own, since
 * each one re-implements that check. A single endpoint that resolves the pin
 * without scoping it to the caller hands somebody else's pin its contents, and
 * the only way to notice is to ask all of them the same question.
 */

import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";

/** The child collections that live under a pin, and a valid body for each. */
const COLLECTIONS = [
    { name: "links", path: "links", payload: () => ({ url: "https://example.invalid/urbanlens-suite", name: resourceName("link") }) },
    { name: "notes", path: "notes", payload: () => ({ text: resourceName("note") }) },
    { name: "comments", path: "comments", payload: () => ({ text: resourceName("comment") }) },
] as const;

test.describe("pin contents", () => {
    for (const collection of COLLECTIONS) {
        test(`a ${collection.name} entry can be added to a pin and read back`, async ({ api }) => {
            const pin = await api.createPin({ name: resourceName(`${collection.name} host`) });
            const payload = collection.payload();

            const created = await api.post(`pins/${pin.slug}/${collection.path}/`, payload);
            expect(created.status(), `creating a ${collection.name} entry answered ${created.status()}: ${(await created.text()).slice(0, 250)}`).toBeLessThan(300);

            const listed = await api.get(`pins/${pin.slug}/${collection.path}/`);
            expect(listed.status(), `listing ${collection.name} answered ${listed.status()}`).toBe(200);

            // Matched on a value from the payload rather than on an id, because
            // each collection names its identifier differently and the point is
            // that the row is *there*.
            const marker = String(Object.values(payload).find((value) => typeof value === "string" && value.startsWith("e2e-")) ?? "");
            expect(marker, "the payload carried no run-scoped marker to search for").toBeTruthy();
            expect(await listed.text(), `a ${collection.name} entry just created is not in the list`).toContain(marker);
        });

        test(`a ${collection.name} entry cannot be added to a pin slug that does not exist`, async ({ api }) => {
            const response = await api.post(`pins/definitely-not-a-real-pin-slug-91b2c/${collection.path}/`, collection.payload());
            expect(response.status(), `adding a ${collection.name} entry to an unknown pin answered ${response.status()}`).toBe(404);
            expect(await response.json()).toHaveProperty("error");
        });
    }

    test("a link must actually be a url", async ({ api }) => {
        const pin = await api.createPin({ name: resourceName("link validation") });
        const response = await api.post(`pins/${pin.slug}/links/`, { url: "not a url at all", name: resourceName("bad link") });

        expect(response.status(), `a malformed url answered ${response.status()}`).toBe(400);
        expect(await response.json()).toHaveProperty("error");
    });

    test("a comment can carry a reaction", async ({ api }) => {
        const pin = await api.createPin({ name: resourceName("reaction host") });
        const created = await api.post(`pins/${pin.slug}/comments/`, { text: resourceName("reacted to") });
        expect(created.status()).toBeLessThan(300);
        const comment = (await created.json()) as { id?: number; comment_id?: number };
        const id = comment.id ?? comment.comment_id;
        expect(id, `the created comment carries no id: ${JSON.stringify(comment).slice(0, 200)}`).toBeTruthy();

        // The emoji is in the path, so this is also a check that a non-ASCII
        // path segment survives the proxy and Django's URL resolver - which is
        // the kind of thing that works locally and 404s behind nginx.
        const reacted = await api.put(`pins/${pin.slug}/comments/${id}/reactions/%F0%9F%91%8D/`, {});
        expect(reacted.status(), `reacting answered ${reacted.status()}: ${(await reacted.text()).slice(0, 200)}`).toBeLessThan(300);

        const removed = await api.delete(`pins/${pin.slug}/comments/${id}/reactions/%F0%9F%91%8D/`);
        expect(removed.ok(), `removing the reaction answered ${removed.status()}`).toBeTruthy();
    });

    ifSecondaryAccount()("another account's pin does not accept contents", async ({ api, secondaryApi }) => {
        // The check each of these collections re-implements. One that resolves
        // the pin without scoping it to the caller lets anybody write into
        // somebody else's pin.
        const theirs = await secondaryApi.createPin({ name: resourceName("not yours") });

        for (const collection of COLLECTIONS) {
            const response = await api.post(`pins/${theirs.slug}/${collection.path}/`, collection.payload());
            expect(
                response.status(),
                `writing a ${collection.name} entry into another account's pin answered ${response.status()} rather than 404`,
            ).toBe(404);
        }
    });

    test("a key without the pins scope cannot write pin contents", async ({ api, restrictedApi }) => {
        const pin = await api.createPin({ name: resourceName("scope guarded contents") });
        const response = await restrictedApi.post(`pins/${pin.slug}/notes/`, { text: "should not be accepted" });
        expect(response.status(), "a profile:read key wrote a pin note").toBe(403);
    });
});
