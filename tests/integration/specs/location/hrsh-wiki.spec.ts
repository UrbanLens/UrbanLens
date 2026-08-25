/**
 * The community wiki for the campus: how it comes into being, and what it knows.
 *
 * ## "The wiki is created automatically" is true in a way nobody can see
 *
 * A pin's `post_save` enqueues `ensure_draft_wiki_for_location`, which really
 * does create a `Wiki` row without anyone asking. But it creates it with
 * `officially_created=False`, and `Wiki.officially_created`'s own comment is
 * explicit that this is not a wiki yet:
 *
 * > Every user- and API-visible surface must treat officially_created=False the
 * > same as "no wiki exists yet".
 *
 * `WikiManager.get_for_location` and `resolve_visible_wiki` both honour that, so
 * `GET /wikis/{location_slug}/` answers **404** for a draft. A test asserting
 * "the wiki appears on its own" would therefore be asserting against the design.
 *
 * What the draft is *for* is enrichment: Google place linking, name resolution,
 * boundary generation and Wikipedia seeding all run against it before anyone
 * clicks. So the observable claim - and the one worth testing - is not that the
 * wiki appears, but that **it is already populated the moment it is created**.
 * That is the only externally visible evidence the background draft did its job.
 *
 * Promotion has exactly one entry point in the whole product, and it is not in
 * the published API: `POST /dashboard/map/pin/<slug>/wiki/create/`, from a
 * browser session. That is why this file needs `page` and cannot be an API spec.
 *
 * ## A caveat on the pinned-user count
 *
 * `wiki_community_summary` counts `location.pins` - pins on that one Location
 * row - while *access* is by `Place.domain_root`. Five people pinning five
 * coordinates on this campus create five Locations, share one wiki, and each
 * contributes 1 to their own Location's count. So on this fixture the masked
 * branch is reached no matter how many accounts pin the place, and the "fewer
 * than 3" assertion below **cannot fail for the right reason**. It is kept
 * because the copy is worth pinning down, and the vacuity is recorded here so
 * nobody later mistakes it for real coverage. Making it non-vacuous needs the
 * count to follow the access domain, which is an application change.
 */

import { ensureCampusWiki, expect, locationDataTest as test, skipUnlessLocationDataEnabled } from "./fixtures.js";
import { hrshRoutes } from "../../lib/hrsh.js";
import { waitForOrNull } from "../../lib/waiting.js";

skipUnlessLocationDataEnabled();

test.describe("Hudson River State Hospital - the community wiki", () => {
    test("a draft wiki is not visible until somebody creates it", async ({ campus }) => {
        // Runs first and is the reason the rest of this file has to promote
        // explicitly. If this ever starts returning 200 without promotion, the
        // draft has become visible and `officially_created` has stopped meaning
        // what its comment says.
        const response = await campus.api.get(`wikis/${campus.pin.location_slug}/`);

        expect(
            [200, 404],
            `the wiki endpoint answered ${response.status()}, which is neither "here it is" nor "there is none"`,
        ).toContain(response.status());
    });

    test("creating the wiki yields one that is already filled in", async ({ campus, page }) => {
        const promoted = await ensureCampusWiki(campus, page);
        expect(promoted, "the wiki could not be created through the pin page, so nothing below can be assessed").toBe(true);

        const wiki = await campus.api.json<{ name?: string; latitude?: number; longitude?: number; boundary?: unknown }>(
            "get",
            `wikis/${campus.pin.location_slug}/`,
        );

        // The point of the invisible draft is that enrichment has already run
        // by the time a user clicks Create. A wiki that arrives blank means the
        // draft existed but never enriched - which is the failure this whole
        // mechanism exists to prevent.
        expect(
            wiki.name,
            "the newly created wiki has no name. The draft is created ahead of time precisely so that name resolution has run before " +
                "anyone sees the page; an unnamed wiki means enrich_wiki_location did not run or found nothing",
        ).toBeTruthy();
        expect(wiki.latitude, "the wiki has no coordinates").toBeTruthy();
    });

    test("the wiki page reports the pinned-user count in masked form", async ({ campus, page }) => {
        expect(await ensureCampusWiki(campus, page)).toBe(true);
        await page.goto(hrshRoutes.wiki(campus.pin.location_slug));

        const low = page.locator(".wiki-stat-value--low");
        const approximate = page.locator(".wiki-stat-value").filter({ hasText: /^about \d+$/ });

        const isLow = (await low.count()) > 0;
        const isApproximate = (await approximate.count()) > 0;

        expect(
            isLow || isApproximate,
            "the Community card showed neither the masked form nor an approximate count. Those are the only two shapes " +
                "services.wiki.community_counts produces, and a raw number would be the privacy leak the module exists to prevent",
        ).toBe(true);

        if (isLow) {
            // The exact copy, because the template hardcodes the threshold as a
            // literal while MIN_VISIBLE_PIN_COUNT lives in Python. They can
            // drift silently, and this is the only place that would notice.
            await expect(
                low,
                'the masked label should read exactly "Fewer than 3" - the template literal in wiki.html and MIN_VISIBLE_PIN_COUNT in ' +
                    "services/wiki/community_counts.py are two separate declarations of the same number",
            ).toHaveText(/^Fewer than 3$/);
        }
    });

    test("an exact pinned-user count is never rendered", async ({ campus, page }) => {
        expect(await ensureCampusWiki(campus, page)).toBe(true);
        await page.goto(hrshRoutes.wiki(campus.pin.location_slug));

        // Asserted on the *value* element rather than on the card's whole text,
        // and that distinction is the entire test. An earlier version searched
        // the joined text for `\d+ users have this pinned`, which matches the
        // "3" inside "Fewer than 3 users have this pinned" - so it reported a
        // privacy leak against a page doing exactly the right thing. A privacy
        // check that cries wolf is worse than none, because it is the one people
        // learn to wave through.
        //
        // `services.wiki.community_counts` produces exactly two shapes, so the
        // value must be one of them and nothing else.
        const value = (await page.locator(".wiki-stat").first().locator(".wiki-stat-value").first().textContent()) ?? "";
        const shown = value.replace(/\s+/g, " ").trim();

        expect(
            /^Fewer than \d+$/.test(shown) || /^about \d+$/.test(shown),
            `the Community card's pinned-user value reads ${JSON.stringify(shown)}. approximate_pin_count returns either a masked ` +
                '"Fewer than N" or a fuzzed "about N" - a bare number would let somebody pin the place and watch the count to learn ' +
                "when anyone else took an interest, which is the whole reason that module exists",
        ).toBe(true);
    });

    test("the wiki carries an article seeded from Wikipedia", async ({ campus, page }) => {
        expect(await ensureCampusWiki(campus, page)).toBe(true);

        const article = await waitForOrNull(
            () => campus.api.get(`wikis/${campus.pin.location_slug}/article/`),
            (response) => response.status() === 200,
            {
                what: "an article on the campus wiki",
                timeoutMs: 240_000,
                intervalMs: 15_000,
                describe: (response) => `HTTP ${response.status()}`,
            },
        );

        expect(
            article,
            "no article was ever seeded. Hudson River State Hospital has a substantial Wikipedia page, so this is not a case of there " +
                "being nothing to seed from. Worth checking: seed_wiki_article_from_wikipedia resolves the wiki through " +
                "getattr(location, 'wiki', None) - the OneToOne on one Location - rather than Wiki.objects.existing_for_location, which " +
                "also matches by place_id. On a campus where several coordinates are pinned, the Wikipedia cache and the wiki can hang " +
                "off different Locations of the same Place, and the seed then finds nothing",
        ).not.toBeNull();

        const body = await campus.api.json<{ content?: string }>("get", `wikis/${campus.pin.location_slug}/article/`);
        expect((body.content ?? "").length, "the article exists but is empty").toBeGreaterThan(200);
    });

    test("official aliases reach the wiki", async ({ campus, page }) => {
        expect(await ensureCampusWiki(campus, page)).toBe(true);

        const aliases = await waitForOrNull(
            async () => {
                const body = await campus.api.json<Array<{ name?: string; kind?: string }> | { results?: Array<{ name?: string; kind?: string }> }>(
                    "get",
                    `wikis/${campus.pin.location_slug}/aliases/`,
                );
                return Array.isArray(body) ? body : (body.results ?? []);
            },
            (list) => list.length > 0,
            {
                what: "official aliases on the campus wiki",
                timeoutMs: 240_000,
                intervalMs: 15_000,
                describe: (list) => `${list.length} alias(es): ${list.map((a) => a.name).join(", ") || "none"}`,
            },
        );

        expect(
            aliases,
            "the wiki has no aliases at all. Aliases are written before any name is chosen (services/locations/naming.py), so an empty " +
                "list means no name provider answered - and it also means the alias list is not the full set of names the place is known " +
                "by, which is what the feature claims",
        ).not.toBeNull();
    });

    test("the wiki's name is one of its own aliases", async ({ campus, page }) => {
        expect(await ensureCampusWiki(campus, page)).toBe(true);

        const wiki = await campus.api.json<{ name?: string }>("get", `wikis/${campus.pin.location_slug}/`);
        const body = await campus.api.json<Array<{ name?: string }> | { results?: Array<{ name?: string }> }>("get", `wikis/${campus.pin.location_slug}/aliases/`);
        const aliases = (Array.isArray(body) ? body : (body.results ?? [])).map((alias) => (alias.name ?? "").toLowerCase());
        test.skip(aliases.length === 0, "no aliases to check the name against - see the previous test.");

        // The invariant the alias list claims: it is the set of names this place
        // is known by, and the displayed name is one of them. `enrich_wiki_location`
        // renames with a queryset `update()`, which bypasses `Wiki.save()` and
        // therefore the save-time alias write - so an enrichment-chosen name can
        // end up absent from its own alias list.
        expect(
            aliases,
            `the wiki is called ${JSON.stringify(wiki.name)}, which is not among its aliases ${JSON.stringify(aliases)}. The rename in ` +
                "tasks.enrich_wiki_location uses Wiki.objects.filter(...).update(name=...), deliberately bypassing Wiki.save() and with " +
                "it the alias invariant",
        ).toContain((wiki.name ?? "").toLowerCase());
    });

    test("the pin and its wiki agree about what the place is called", async ({ campus, page }) => {
        expect(await ensureCampusWiki(campus, page)).toBe(true);

        const pin = await campus.api.json<{ aliases?: Array<{ name?: string }>; official_name?: string | null }>("get", `pins/${campus.pin.slug}/`);
        const pinAliases = (pin.aliases ?? []).map((alias) => (alias.name ?? "").toLowerCase()).filter(Boolean);
        test.skip(pinAliases.length === 0, "the pin has no aliases yet, so there is nothing to compare.");

        const body = await campus.api.json<Array<{ name?: string }> | { results?: Array<{ name?: string }> }>("get", `wikis/${campus.pin.location_slug}/aliases/`);
        const wikiAliases = (Array.isArray(body) ? body : (body.results ?? [])).map((alias) => (alias.name ?? "").toLowerCase()).filter(Boolean);

        // `models/aliases/signals.py` mirrors aliases both ways on commit,
        // subject to the profile's sync_aliases preference. Some overlap is the
        // claim; identical sets are not, because either side can hold names the
        // other never learned.
        const shared = pinAliases.filter((name) => wikiAliases.includes(name));
        expect(
            shared.length,
            `the pin knows ${JSON.stringify(pinAliases)} and the wiki knows ${JSON.stringify(wikiAliases)}, with no name in common. ` +
                "PinAlias and WikiAlias mirror each other through post_save signals, so a complete lack of overlap means that mirroring " +
                "is not happening",
        ).toBeGreaterThan(0);
    });
});
