/**
 * The community wiki: reading it, editing it, and losing an edit race. The concurrency check is the
 * reason this file exists.
 */

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import { MISSING_SLUG, uniqueMarker } from "../../lib/security.js";
import { locationSlugOf, pinWithWiki, placelessCoordinates, readArticle, waitForWiki } from "../../lib/wiki.js";

interface Paginated<T> {
    count: number;
    results: T[];
}

interface Revision {
    id: number;
    edit_summary: string;
}

interface SavedArticle {
    content: string;
    base_revision_id: number | null;
}

test.describe("wiki", () => {
    test("a pinned location's wiki appears without any promotion step, and a missing one is a clean 404", async ({ api }) => {
        test.slow();
        const pin = await api.createPin({ name: resourceName("wiki host"), ...placelessCoordinates() });
        const slug = await locationSlugOf(api, pin.slug);

        // Before the background task lands, only 200 or 404 is acceptable: 403 would leak that the
        // wiki exists but is not yours, 500 is how an unresolvable location used to behave.
        const first = await api.get(`wikis/${slug}/`);
        expect([200, 404], `the wiki for a location this account has pinned answered ${first.status()}: ${(await first.text()).slice(0, 200)}`).toContain(first.status());

        const wiki = await waitForWiki(api, slug);
        expect(wiki.location_slug, "the wiki answered for a different location than the one asked for").toBe(slug);

        const missing = await api.get(`wikis/${MISSING_SLUG}/`);
        expect(missing.status(), `a location that does not exist answered ${missing.status()}`).toBe(404);
        expect(await missing.json(), "a missing wiki did not use the standard error envelope").toHaveProperty("error");
    });

    test("an article can be written and read back, and a save without a base revision is refused", async ({ api }) => {
        test.slow();
        const { locationSlug } = await pinWithWiki(api, { name: resourceName("article host") });
        const before = await readArticle(api, locationSlug);

        const unbased = uniqueMarker("wikiunbased");
        const refused = await api.put(`wikis/${locationSlug}/article/`, { content: unbased, edit_summary: "no base" });
        expect(refused.status(), `a save omitting base_revision_id answered ${refused.status()}; it has to be refused rather than treated as "no opinion"`).toBe(400);

        const content = `Written by the UrbanLens integration suite (${uniqueMarker("wikiarticle")}).`;
        const saved = await api.put(`wikis/${locationSlug}/article/`, {
            content,
            base_revision_id: before?.base_revision_id ?? null,
            edit_summary: "integration suite",
        });
        expect(saved.status(), `saving the article answered ${saved.status()}: ${(await saved.text()).slice(0, 300)}`).toBe(200);

        const after = await readArticle(api, locationSlug);
        expect(after?.content, "the article read back is not what was just written").toContain(content);
        expect(after?.content, "the refused save's text reached the article anyway").not.toContain(unbased);
    });

    test("an edit quoting a stale revision is refused rather than silently winning", async ({ api }) => {
        test.slow();
        const { locationSlug } = await pinWithWiki(api, { name: resourceName("edit race host") });
        const base = (await readArticle(api, locationSlug))?.base_revision_id ?? null;

        const first = await api.put(`wikis/${locationSlug}/article/`, { content: "First writer's text.", base_revision_id: base, edit_summary: "first" });
        expect(first.status(), `the first save answered ${first.status()}: ${(await first.text()).slice(0, 200)}`).toBe(200);
        const current = ((await first.json()) as SavedArticle).base_revision_id;
        expect(current, "the first save did not produce a new revision, so the second writer's base is not actually stale").not.toBe(base);

        // The second writer loaded the page before the first saved, so it still quotes the older revision.
        const stale = await api.put(`wikis/${locationSlug}/article/`, { content: "Second writer's text, based on a stale read.", base_revision_id: base, edit_summary: "second" });
        expect(stale.status(), `an edit quoting a superseded base_revision_id answered ${stale.status()}, which means concurrent edits overwrite each other silently`).toBe(409);
        expect(await stale.json(), "the conflict did not name the revision that won, so the client cannot rebase").toMatchObject({ conflict: true, current_revision_id: current });

        const surviving = await readArticle(api, locationSlug);
        expect(surviving?.content, "the losing writer's text overwrote the winner's").toContain("First writer's text.");
        expect(surviving?.content, "the losing writer's text was merged into the article").not.toContain("Second writer's text");
    });

    test("edits are recorded in the history", async ({ api }) => {
        test.slow();
        const { locationSlug } = await pinWithWiki(api, { name: resourceName("history host") });
        const summary = uniqueMarker("wikiaudited");
        const description = uniqueMarker("wikidesc");

        const historyBefore = await api.json<Paginated<unknown>>("get", `wikis/${locationSlug}/history/`);
        expect(JSON.stringify(historyBefore.results), "the field-edit history already mentions the description this test is about to write").not.toContain(description);

        const saved = await api.put(`wikis/${locationSlug}/article/`, {
            content: "A recorded edit.",
            base_revision_id: (await readArticle(api, locationSlug))?.base_revision_id ?? null,
            edit_summary: summary,
        });
        expect(saved.status(), `saving the article answered ${saved.status()}`).toBe(200);
        const revisionId = ((await saved.json()) as SavedArticle).base_revision_id;

        // A community-editable document with no attribution trail is not moderatable.
        const revisions = await api.json<Paginated<Revision>>("get", `wikis/${locationSlug}/article/revisions/`);
        expect(revisions.results.find((revision) => revision.id === revisionId)?.edit_summary, "the saved revision is missing from the article's revision list").toBe(summary);

        const patched = await api.patch(`wikis/${locationSlug}/`, { description });
        expect(patched.status(), `editing the wiki description answered ${patched.status()}: ${(await patched.text()).slice(0, 200)}`).toBe(200);
        const historyAfter = await api.json<Paginated<unknown>>("get", `wikis/${locationSlug}/history/`);
        expect(JSON.stringify(historyAfter.results), "a field edit left no row in the wiki's history").toContain(description);

        const unknownEdit = await api.post(`wikis/${locationSlug}/history/2147483647/revert/`);
        expect(unknownEdit.status(), `reverting an edit that does not exist answered ${unknownEdit.status()}`).toBe(404);
    });

    test("a comment can be posted and read back, and an empty one is refused", async ({ api }) => {
        test.slow();
        const { locationSlug } = await pinWithWiki(api, { name: resourceName("comment host") });

        const empty = await api.post(`wikis/${locationSlug}/comments/`, { text: "   " });
        expect(empty.status(), `an empty comment answered ${empty.status()}`).toBe(400);

        const text = resourceName(uniqueMarker("wikicomment"));
        const posted = await api.post(`wikis/${locationSlug}/comments/`, { text });
        expect(posted.status(), `posting a comment answered ${posted.status()}: ${(await posted.text()).slice(0, 200)}`).toBe(201);

        const listed = await api.json<Paginated<{ text: string }>>("get", `wikis/${locationSlug}/comments/`);
        expect(listed.results.map((comment) => comment.text), "a comment just posted is not in the comment list").toContain(text);
        expect(listed.results.filter((comment) => comment.text.trim() === ""), "the refused empty comment was stored anyway").toHaveLength(0);
    });

    test("a key without the wiki scope cannot comment, while the full key can", async ({ api, restrictedApi }) => {
        test.slow();
        const { locationSlug } = await pinWithWiki(api, { name: resourceName("scope host") });

        const refusedText = uniqueMarker("wikiscoperefused");
        const response = await restrictedApi.post(`wikis/${locationSlug}/comments/`, { text: refusedText });
        expect(response.status(), "a profile:read key commented on a wiki").toBe(403);

        const allowedText = uniqueMarker("wikiscopeallowed");
        const allowed = await api.post(`wikis/${locationSlug}/comments/`, { text: allowedText });
        expect(allowed.status(), `the same account's full key could not comment (${allowed.status()}), so the 403 above may not be about scope`).toBe(201);

        const listed = await (await api.get(`wikis/${locationSlug}/comments/`)).text();
        expect(listed, "the full key's comment is missing").toContain(allowedText);
        expect(listed, "the under-scoped key's comment was stored despite the 403").not.toContain(refusedText);
    });
});
