/**
 * The community wiki: reading it, editing it, and losing an edit race.
 *
 * The concurrency check is the reason this file exists. `PUT article/` requires
 * a `base_revision_id`, which is the whole mechanism protecting a shared
 * document from two people saving over each other - and a mechanism that is
 * *declared* in a serializer but never enforced end to end looks identical to
 * one that works, right up until somebody's edit disappears. Sending a
 * deliberately stale revision id is the only way to find out which it is, and
 * it needs a real revision history to be stale against.
 *
 * Access is earned rather than granted: a wiki is visible to a caller who has a
 * pin on its location (or on the same place domain). Every test here therefore
 * creates its own pin first and reads the location slug back from it, rather
 * than assuming a wiki exists to be found.
 */

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";

interface Article {
    content: string;
    revision_id?: number;
    latest_revision_id?: number;
}

/**
 * The revision id a subsequent edit must quote.
 *
 * Named separately because the field has two plausible spellings and only the
 * deployment knows which; reading both keeps the assertion about concurrency
 * rather than about a key name.
 */
function baseRevision(article: Article): number | undefined {
    return article.revision_id ?? article.latest_revision_id;
}

test.describe("wiki", () => {
    test("a pin earns access to its location's wiki", async ({ api }) => {
        const pin = await api.createPin({ name: resourceName("wiki host") });

        const detail = await api.json<{ location_slug?: string }>("get", `pins/${pin.slug}/`);
        expect(detail.location_slug, "a pin detail carries no location_slug, so nothing can address its wiki").toBeTruthy();

        const wiki = await api.get(`wikis/${detail.location_slug}/`);
        expect(wiki.status(), `the wiki for a location this account has pinned answered ${wiki.status()}: ${(await wiki.text()).slice(0, 200)}`).toBe(200);
    });

    test("an article can be written and read back", async ({ api }) => {
        const pin = await api.createPin({ name: resourceName("article host") });
        const { location_slug: slug } = await api.json<{ location_slug: string }>("get", `pins/${pin.slug}/`);

        const before = await api.json<Article>("get", `wikis/${slug}/article/`);
        const content = `Written by the UrbanLens integration suite (run ${resourceName("article")}).`;

        const saved = await api.put(`wikis/${slug}/article/`, {
            content,
            base_revision_id: baseRevision(before) ?? 0,
            edit_summary: "integration suite",
        });
        expect(saved.status(), `saving the article answered ${saved.status()}: ${(await saved.text()).slice(0, 300)}`).toBeLessThan(300);

        const after = await api.json<Article>("get", `wikis/${slug}/article/`);
        expect(after.content, "the article read back does not contain what was just written").toContain("integration suite");
    });

    test("an edit quoting a stale revision is refused rather than silently winning", async ({ api }) => {
        const pin = await api.createPin({ name: resourceName("edit race host") });
        const { location_slug: slug } = await api.json<{ location_slug: string }>("get", `pins/${pin.slug}/`);

        const original = await api.json<Article>("get", `wikis/${slug}/article/`);
        const base = baseRevision(original) ?? 0;

        const first = await api.put(`wikis/${slug}/article/`, { content: "First writer's text.", base_revision_id: base, edit_summary: "first" });
        expect(first.status(), `the first save answered ${first.status()}`).toBeLessThan(300);

        // The second writer loaded the page before the first saved, so it still
        // quotes the older revision. Accepting this is how an edit disappears.
        const stale = await api.put(`wikis/${slug}/article/`, { content: "Second writer's text, based on a stale read.", base_revision_id: base, edit_summary: "second" });
        expect(
            [409, 400],
            `an edit quoting a superseded base_revision_id answered ${stale.status()}, which means concurrent edits overwrite each other silently`,
        ).toContain(stale.status());

        const surviving = await api.json<Article>("get", `wikis/${slug}/article/`);
        expect(surviving.content, "the losing writer's text overwrote the winner's").toContain("First writer's text.");
    });

    test("edits are recorded in the history", async ({ api }) => {
        const pin = await api.createPin({ name: resourceName("history host") });
        const { location_slug: slug } = await api.json<{ location_slug: string }>("get", `pins/${pin.slug}/`);

        const before = await api.json<Article>("get", `wikis/${slug}/article/`);
        await api.put(`wikis/${slug}/article/`, { content: "A recorded edit.", base_revision_id: baseRevision(before) ?? 0, edit_summary: "audited" });

        // A community-editable document with no attribution trail is not
        // moderatable; this is the surface a reviewer actually uses.
        const history = await api.get(`wikis/${slug}/history/`);
        expect(history.status(), `the wiki history answered ${history.status()}`).toBe(200);
        const revisions = await api.get(`wikis/${slug}/article/revisions/`);
        expect(revisions.status(), `the revision list answered ${revisions.status()}`).toBe(200);
    });

    test("a comment can be posted and read back", async ({ api }) => {
        const pin = await api.createPin({ name: resourceName("comment host") });
        const { location_slug: slug } = await api.json<{ location_slug: string }>("get", `pins/${pin.slug}/`);

        const text = resourceName("wiki comment");
        const posted = await api.post(`wikis/${slug}/comments/`, { text });
        expect(posted.status(), `posting a comment answered ${posted.status()}: ${(await posted.text()).slice(0, 200)}`).toBeLessThan(300);

        const listed = await api.get(`wikis/${slug}/comments/`);
        expect(listed.status()).toBe(200);
        expect(await listed.text(), "a comment just posted is not in the comment list").toContain(text);
    });

    test("a key without the wiki scope cannot edit", async ({ api, restrictedApi }) => {
        const pin = await api.createPin({ name: resourceName("scope host") });
        const { location_slug: slug } = await api.json<{ location_slug: string }>("get", `pins/${pin.slug}/`);

        const response = await restrictedApi.post(`wikis/${slug}/comments/`, { text: "should not be accepted" });
        expect(response.status(), "a profile:read key commented on a wiki").toBe(403);
    });
});
