/**
 * Wiki access is earned only by pinning the place (docs/GOALS.md, "Wiki access"). A wiki the viewer
 * has not earned must be indistinguishable from one that does not exist, on every route, and must
 * never surface through search.
 */

import type { APIResponse } from "@playwright/test";

import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import { expectIndistinguishableFromMissing, MISSING_SLUG, uniqueMarker } from "../../lib/security.js";
import { csrfHeaders, expectPageIndistinguishableFromMissing, pinWithWiki, readArticle, writeArticle } from "../../lib/wiki.js";

/** Every read route under `wikis/{location_slug}/` (external_api/urls.py, urls_wiki_extra.py, urls_wiki_community.py). */
const API_READ_ROUTES = ["", "history/", "aliases/", "links/", "gallery/", "article/", "article/revisions/", "comments/", "boundary/", "ownership/", "sales/", "votes/danger/"] as const;

/** Session-authenticated wiki pages and fragments under `/dashboard/location/{slug}/wiki/` (dashboard/urls.py). */
const WEB_READ_ROUTES = ["", "history/", "article/", "comments/", "boundary/", "detail-pins/json/", "ownership/", "sales/"] as const;

function webWikiPath(locationSlug: string, suffix: string): string {
    return `/dashboard/location/${locationSlug}/wiki/${suffix}`;
}

interface SearchGroups {
    groups?: unknown[];
}

async function groupsOf(response: APIResponse): Promise<string> {
    expect(response.status(), `search/ answered ${response.status()}: ${(await response.text()).slice(0, 200)}`).toBe(200);
    return JSON.stringify(((await response.json()) as SearchGroups).groups ?? []);
}

async function resultsOf(response: APIResponse, what: string): Promise<unknown[]> {
    expect(response.status(), `${what} answered ${response.status()}: ${(await response.text()).slice(0, 200)}`).toBe(200);
    return ((await response.json()) as { results?: unknown[] }).results ?? [];
}

test.describe("a wiki the viewer has not earned does not exist for them", () => {
    ifSecondaryAccount()("an unearned wiki is absent on every API route, read and write", async ({ api, secondaryApi }) => {
        test.slow();
        const { locationSlug } = await pinWithWiki(api, { name: resourceName("wiki access api") });

        const articleMarker = uniqueMarker("waapiart");
        const saved = await writeArticle(api, locationSlug, `Owner's article ${articleMarker}`);
        expect(saved.status(), `the owner could not write the article (${saved.status()}), so article/ would 404 for everyone and prove nothing`).toBe(200);

        const ownerFailures: string[] = [];
        for (const route of API_READ_ROUTES) {
            const response = await api.get(`wikis/${locationSlug}/${route}`);
            if (response.status() !== 200) {
                ownerFailures.push(`GET wikis/{slug}/${route} -> ${response.status()}`);
            }
        }
        expect(ownerFailures, "the owner, who earned this wiki by pinning it, could not read these routes, so the stranger's 404s below would prove nothing").toEqual([]);

        for (const route of API_READ_ROUTES) {
            await expectIndistinguishableFromMissing(
                await secondaryApi.get(`wikis/${locationSlug}/${route}`),
                await secondaryApi.get(`wikis/${MISSING_SLUG}/${route}`),
                `a stranger's GET wikis/{slug}/${route}`,
            );
        }

        const writeMarker = uniqueMarker("waapiwrite");
        const writes: Array<{ label: string; send: (slug: string) => Promise<APIResponse> }> = [
            { label: "PATCH wikis/{slug}/", send: (slug) => secondaryApi.patch(`wikis/${slug}/`, { description: writeMarker }) },
            { label: "POST comments/", send: (slug) => secondaryApi.post(`wikis/${slug}/comments/`, { text: writeMarker }) },
            { label: "PUT article/", send: (slug) => secondaryApi.put(`wikis/${slug}/article/`, { content: writeMarker, base_revision_id: null }) },
            { label: "POST aliases/", send: (slug) => secondaryApi.post(`wikis/${slug}/aliases/`, { name: writeMarker }) },
            { label: "POST links/", send: (slug) => secondaryApi.post(`wikis/${slug}/links/`, { name: writeMarker, url: `https://example.invalid/${writeMarker}` }) },
            { label: "PUT votes/danger/", send: (slug) => secondaryApi.put(`wikis/${slug}/votes/danger/`, { value: 5 }) },
            { label: "DELETE cover-photo/", send: (slug) => secondaryApi.delete(`wikis/${slug}/cover-photo/`) },
        ];
        for (const write of writes) {
            await expectIndistinguishableFromMissing(await write.send(locationSlug), await write.send(MISSING_SLUG), `a stranger's ${write.label}`);
        }

        const after = await api.json<{ description: string | null }>("get", `wikis/${locationSlug}/`);
        expect(after.description ?? "", "a stranger's refused PATCH still changed the wiki's description").not.toContain(writeMarker);
        for (const route of ["comments/", "aliases/", "links/"]) {
            expect(await (await api.get(`wikis/${locationSlug}/${route}`)).text(), `a stranger's refused write still landed in ${route}`).not.toContain(writeMarker);
        }
        const article = await readArticle(api, locationSlug);
        expect(article?.content, "a stranger's refused article save replaced the owner's article").toContain(articleMarker);
        const danger = await api.json<{ exact: number | null }>("get", `wikis/${locationSlug}/votes/danger/`);
        expect(danger.exact, "a stranger's refused danger vote was counted in the owner's composite").toBeNull();
    });

    ifSecondaryAccount()("an unearned wiki is absent on every web route, read and write", async ({ api, page, secondaryPage }) => {
        test.slow();
        const { locationSlug, wiki } = await pinWithWiki(api, { name: resourceName("wiki access web") });
        const slugs = [locationSlug, MISSING_SLUG];

        const ownerFailures: string[] = [];
        for (const route of WEB_READ_ROUTES) {
            const response = await page.request.get(webWikiPath(locationSlug, route));
            if (response.status() >= 400) {
                ownerFailures.push(`GET ${webWikiPath("{slug}", route)} -> ${response.status()}`);
            }
        }
        expect(ownerFailures, "the owner could not load these wiki pages, so the stranger's 404s below would prove nothing").toEqual([]);

        for (const route of WEB_READ_ROUTES) {
            const foreign = await secondaryPage.request.get(webWikiPath(locationSlug, route));
            const missing = await secondaryPage.request.get(webWikiPath(MISSING_SLUG, route));
            await expectPageIndistinguishableFromMissing(foreign, missing, slugs, `a stranger's GET ${webWikiPath("{slug}", route)}`);
            expect(await foreign.text(), `a stranger's 404 for ${route || "the wiki page"} still embeds the wiki's uuid`).not.toContain(wiki.uuid);
        }

        const marker = uniqueMarker("waweb");
        const headers = await csrfHeaders(secondaryPage);
        const posts: Array<{ label: string; send: (slug: string) => Promise<APIResponse> }> = [
            {
                label: "POST wiki/edit/",
                send: (slug) => secondaryPage.request.post(webWikiPath(slug, "edit/"), { headers: { ...headers, "Content-Type": "application/json" }, data: { description: marker } }),
            },
            { label: "POST wiki/public-vote/", send: (slug) => secondaryPage.request.post(webWikiPath(slug, "public-vote/"), { headers, form: { choice: "yes" } }) },
            { label: "POST wiki/comments/", send: (slug) => secondaryPage.request.post(webWikiPath(slug, "comments/"), { headers, form: { text: marker } }) },
        ];
        for (const post of posts) {
            // The missing-slug control answering 404 rather than 403 is also what proves CSRF was satisfied.
            await expectPageIndistinguishableFromMissing(await post.send(locationSlug), await post.send(MISSING_SLUG), slugs, `a stranger's ${post.label}`);
        }

        const after = await api.json<{ description: string | null }>("get", `wikis/${locationSlug}/`);
        expect(after.description ?? "", "a stranger's refused web edit still changed the wiki's description").not.toContain(marker);
        expect(await (await api.get(`wikis/${locationSlug}/comments/`)).text(), "a stranger's refused web comment still landed on the wiki").not.toContain(marker);
    });

    ifSecondaryAccount()("an unearned wiki cannot be discovered by any search surface", async ({ api, page, secondaryApi, secondaryPage }) => {
        test.slow();
        const { locationSlug } = await pinWithWiki(api, { name: resourceName("wiki access search") });

        const aliasMarker = uniqueMarker("wasalias");
        const commentMarker = uniqueMarker("wascomment");
        const articleMarker = uniqueMarker("wasarticle");
        const alias = await api.post(`wikis/${locationSlug}/aliases/`, { name: aliasMarker });
        expect(alias.status(), `the owner could not add a wiki alias (${alias.status()}): ${(await alias.text()).slice(0, 200)}`).toBe(201);
        const comment = await api.post(`wikis/${locationSlug}/comments/`, { text: `Owner comment ${commentMarker}` });
        expect(comment.status(), `the owner could not comment on the wiki (${comment.status()}): ${(await comment.text()).slice(0, 200)}`).toBe(201);
        const article = await writeArticle(api, locationSlug, `Owner article ${articleMarker}`);
        expect(article.status(), `the owner could not write the wiki article (${article.status()})`).toBe(200);

        const wikiLink = `/dashboard/location/${locationSlug}/wiki/`;
        for (const marker of [aliasMarker, commentMarker, articleMarker]) {
            expect(await groupsOf(await api.get("search/", { q: marker })), `the owner's search/ for ${marker} did not find their wiki, so the stranger's miss would prove nothing`).toContain(locationSlug);
            const theirs = await groupsOf(await secondaryApi.get("search/", { q: marker }));
            expect(theirs, `a stranger's search/ for ${marker} surfaced a wiki they have not earned`).not.toContain(locationSlug);
            expect(theirs, `a stranger's search/ result rows contain the wiki-only marker ${marker}`).not.toContain(marker);

            const ownerPanel = await page.request.get("/dashboard/search/panel/", { params: { q: marker } });
            expect(ownerPanel.status()).toBe(200);
            expect(await ownerPanel.text(), `the owner's search panel for ${marker} does not link their wiki, so the stranger's miss would prove nothing`).toContain(wikiLink);
            const theirPanel = await secondaryPage.request.get("/dashboard/search/panel/", { params: { q: marker } });
            expect(theirPanel.status()).toBe(200);
            expect(await theirPanel.text(), `a stranger's search panel for ${marker} names the unearned wiki's location`).not.toContain(locationSlug);
        }

        expect(await resultsOf(await api.get("locations/search/", { q: aliasMarker, sources: "local" }), "the owner's locations/search/"), "the owner's locations/search/ found nothing for their wiki's alias").not.toHaveLength(0);
        expect(await resultsOf(await secondaryApi.get("locations/search/", { q: aliasMarker, sources: "local" }), "a stranger's locations/search/"), "a stranger's locations/search/ matched an unearned wiki's alias").toHaveLength(0);

        const autocomplete = "/dashboard/map/search/autocomplete/local/";
        expect(await resultsOf(await page.request.get(autocomplete, { params: { q: aliasMarker } }), "the owner's map autocomplete"), "the owner's map autocomplete found nothing for their wiki's alias").not.toHaveLength(0);
        expect(await resultsOf(await secondaryPage.request.get(autocomplete, { params: { q: aliasMarker } }), "a stranger's map autocomplete"), "a stranger's map autocomplete matched an unearned wiki's alias").toHaveLength(0);
    });

    ifSecondaryAccount()("pinning the exact location grants the wiki, and removing the pin revokes it", async ({ api, secondaryApi }) => {
        test.info().annotations.push({
            type: "decision",
            description:
                "D19 (ruled 2026-09-23): viewing or sharing to a Place-resolved wiki grants access permanently (services/wiki/wiki_access.py:390, services/wiki/wiki_share.py:110). A placeless location has no domain to grant, so this is the exact-location rule. The Place case is covered in pytest (test_grandfathered_parcel_split_access.py): only a paid REData lookup, made by the location project alone, gives a pin a Place.",
        });
        test.slow();
        const { pin, locationSlug, wiki } = await pinWithWiki(api, { name: resourceName("wiki access grant") });
        // Boundary.resolve_for_wiki falls back to a circle only when the location has no Place.
        const boundary = await api.json<{ boundaries: Record<string, { source: string | null }> }>("get", `wikis/${locationSlug}/boundary/`);
        expect(
            boundary.boundaries["property"]?.source,
            "this location's boundary is not the placeless circle fallback, so a Place is in play and D19's engagement grant keeps the visitor's access after unpinning - the revoke half would not test the exact-location rule",
        ).toBe("circle");

        await expectIndistinguishableFromMissing(await secondaryApi.get(`wikis/${locationSlug}/`), await secondaryApi.get(`wikis/${MISSING_SLUG}/`), "the wiki before the stranger pinned the place");

        const theirs = await secondaryApi.createPin({ name: resourceName("wiki access grant visitor"), latitude: pin.latitude, longitude: pin.longitude });
        const granted = await secondaryApi.get(`wikis/${locationSlug}/`);
        expect(granted.status(), `pinning the same coordinates did not grant the wiki (${granted.status()}): ${(await granted.text()).slice(0, 200)}`).toBe(200);
        expect(((await granted.json()) as { uuid: string }).uuid, "the wiki the new pin granted is not the owner's wiki at that location").toBe(wiki.uuid);

        const removed = await secondaryApi.delete(`pins/${theirs.slug}/`);
        expect(removed.status(), `deleting the visitor's pin answered ${removed.status()}`).toBe(204);
        await expectIndistinguishableFromMissing(await secondaryApi.get(`wikis/${locationSlug}/`), await secondaryApi.get(`wikis/${MISSING_SLUG}/`), "the wiki after the visitor removed their only pin there");

        expect((await api.get(`wikis/${locationSlug}/`)).status(), "the owner lost the wiki when a different account unpinned the place").toBe(200);
    });
});
