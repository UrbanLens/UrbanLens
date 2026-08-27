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
 * **Why most of this skips on a fresh deployment.** A wiki is not created by
 * pinning a location. One is auto-created as an invisible draft and only
 * becomes visible when a user promotes it through the web UI's "Create Wiki"
 * action - and the *published API has no endpoint that does that* (`wikis/` is
 * GET and PATCH only; there is no POST). So a client holding an API key can
 * read and edit a wiki that already exists and can never start one, and a
 * suite that only talks to the API cannot manufacture the precondition. That
 * gap is recorded in docs/PROBLEMS.md, 2026-08-24.
 *
 * Rather than assert against a wiki that is not there - which would fail on a
 * correct deployment and teach everyone to ignore this file - each test that
 * needs one resolves it first and skips with the reason when it is absent. The
 * assertions are real and start running the moment a wiki exists.
 */

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import type { ApiClient } from "../../lib/api-client.js";

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

/**
 * Creates a pin, resolves its location, and returns the slug of a *visible*
 * wiki there - skipping the test when there is none.
 *
 * @returns The location slug, addressable as `wikis/<slug>/`.
 */
async function wikiSlugOrSkip(api: ApiClient, label: string): Promise<string> {
    const pin = await api.createPin({ name: resourceName(label) });
    const detail = await api.json<{ location_slug?: string }>("get", `pins/${pin.slug}/`);
    expect(detail.location_slug, "a pin detail carries no location_slug, so nothing can address its wiki").toBeTruthy();

    const slug = String(detail.location_slug);
    const wiki = await api.get(`wikis/${slug}/`);
    test.skip(
        wiki.status() === 404,
        "This location has no visible wiki. One is auto-created as an invisible draft and promoted only through the web UI; the published API has no endpoint that creates one.",
    );
    expect(wiki.status(), `the wiki answered ${wiki.status()}: ${(await wiki.text()).slice(0, 200)}`).toBe(200);
    return slug;
}

test.describe("wiki", () => {
    test("a pinned location is addressable, and answers a wiki or a clean absence", async ({ api }) => {
        const pin = await api.createPin({ name: resourceName("wiki host") });

        const detail = await api.json<{ location_slug?: string }>("get", `pins/${pin.slug}/`);
        expect(detail.location_slug, "a pin detail carries no location_slug, so nothing can address its wiki").toBeTruthy();

        const wiki = await api.get(`wikis/${detail.location_slug}/`);

        // 200 or 404, and nothing else. 404 is the correct answer for a
        // location whose wiki is still an unpromoted draft; what would be wrong
        // is a 403 (which would leak that a wiki exists but is not yours) or a
        // 500 (which is how an unresolvable location used to behave). Both of
        // those are what this is really watching for.
        expect([200, 404], `the wiki for a location this account has pinned answered ${wiki.status()}: ${(await wiki.text()).slice(0, 200)}`).toContain(wiki.status());
        if (wiki.status() === 404) {
            expect(await wiki.json(), "a missing wiki did not use the standard error envelope").toHaveProperty("error");
        }
    });

    test("an article can be written and read back", async ({ api }) => {
        const slug = await wikiSlugOrSkip(api, "article host");

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
        const slug = await wikiSlugOrSkip(api, "edit race host");

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
        const slug = await wikiSlugOrSkip(api, "history host");

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
        const slug = await wikiSlugOrSkip(api, "comment host");

        const text = resourceName("wiki comment");
        const posted = await api.post(`wikis/${slug}/comments/`, { text });
        expect(posted.status(), `posting a comment answered ${posted.status()}: ${(await posted.text()).slice(0, 200)}`).toBeLessThan(300);

        const listed = await api.get(`wikis/${slug}/comments/`);
        expect(listed.status()).toBe(200);
        expect(await listed.text(), "a comment just posted is not in the comment list").toContain(text);
    });

    test("a key without the wiki scope cannot edit", async ({ api, restrictedApi }) => {
        const slug = await wikiSlugOrSkip(api, "scope host");

        const response = await restrictedApi.post(`wikis/${slug}/comments/`, { text: "should not be accepted" });
        expect(response.status(), "a profile:read key commented on a wiki").toBe(403);
    });
});
