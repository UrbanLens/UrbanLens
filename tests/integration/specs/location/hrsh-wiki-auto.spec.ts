/**
 * The community wiki auto-created for the campus: title, article, photos, child wikis, and the
 * private pin's link to its own child wiki. No test here creates or promotes a wiki by hand.
 */

import { HRSH_NAME_PATTERN, hrshRoutes } from "../../lib/hrsh.js";
import { recordMetric } from "../../lib/metrics.js";
import { pinDetail } from "../../lib/routes.js";
import { waitForOrNull } from "../../lib/waiting.js";
import {
    CAMPUS_PRIVATE_NAME,
    ensureCampusWiki,
    expect,
    locationDataTest as test,
    readPin,
    skipUnlessLocationDataEnabled,
    waitForCampusWiki,
    waitForChildPins,
    type CampusFixture,
    type SyncPinRow,
    type WikiDetail,
} from "./fixtures.js";

skipUnlessLocationDataEnabled();

/** Third-party tile hosts the strict console guard would otherwise fail on; see hrsh-media.spec.ts. */
const THIRD_PARTY_TILE_HOSTS = [/wayback\.maptiles\.arcgis\.com/, /tile\.openstreetmap\.org/, /server\.arcgisonline\.com/];

const TITLE_WAIT_MS = 300_000;
const ARTICLE_WAIT_MS = 240_000;
const MEDIA_WAIT_MS = 120_000;

/** CRIS records ~124 buildings on this campus; sampled rather than walked in full for runtime's sake. */
const CHILD_WIKI_SAMPLE = 25;

function isMeaningfulTitle(name: string | null | undefined): boolean {
    return Boolean(name) && HRSH_NAME_PATTERN.test(name!) && !/unnamed location/i.test(name!);
}

/** Whether a wiki name leaks the private pin's own name - it must come from Location/external data only. */
function leaksPrivateName(name: string, campus: CampusFixture): boolean {
    const lower = name.toLowerCase();
    return [CAMPUS_PRIVATE_NAME, campus.nameAtSetup].filter(Boolean).some((candidate) => lower.includes(candidate.toLowerCase()));
}

interface ChildWikiResolution {
    pin: SyncPinRow;
    locationSlug: string;
    wiki: WikiDetail | null;
}

/**
 * One-pass lookup of each sampled building's own wiki.
 *
 * No poll loop: `pin_restructure.mirror_buildings_to_wiki` runs alongside `create_building_pins` from
 * the same building list, so by the time a child pin is visible its wiki (or the mirror's swallowed
 * failure) has already happened.
 */
async function resolveChildWikis(campus: CampusFixture, children: SyncPinRow[]): Promise<ChildWikiResolution[]> {
    const sample = children.slice(0, CHILD_WIKI_SAMPLE);
    return Promise.all(
        sample.map(async (pin) => {
            const detail = await readPin(campus.api, pin.slug);
            const response = await campus.api.get(`wikis/${detail.location_slug}/`);
            const wiki = response.status() === 200 ? ((await response.json()) as WikiDetail) : null;
            return { pin, locationSlug: detail.location_slug, wiki };
        }),
    );
}

test.describe("Hudson River State Hospital - the community wiki, automatically", () => {
    test("a wiki exists with no create/promote action, and its page renders", async ({ campus, page }) => {
        const wiki = await waitForCampusWiki(campus);
        expect(
            wiki,
            `no wiki ever answered at wikis/${campus.pin.location_slug}/. A Pin's post_save signal enqueues tasks.ensure_wiki_for_location ` +
                "for every pin (models/pin/signals.py) - nobody has to create or promote one, so none appearing means that signal, or the " +
                "Celery worker behind it, did not run",
        ).not.toBeNull();

        const response = await page.goto(hrshRoutes.wiki(campus.pin.location_slug));
        const landed = new URL(page.url()).pathname;
        const status = response?.status() ?? 0;
        expect(status >= 200 && status < 300, `the wiki page answered HTTP ${status} instead of rendering`).toBe(true);
        expect(
            landed,
            `landed on ${landed} instead of the wiki page - a redirect usually means the account is signed out or the wiki is not visible to it`,
        ).toBe(hrshRoutes.wiki(campus.pin.location_slug));
        await expect(page.locator(".wiki-title"), "the wiki page rendered without its title element").toBeAttached();
    });

    test("the wiki is titled after the property, never unnamed, and never carries the private pin's name", async ({ campus, page }) => {
        test.skip(!(await ensureCampusWiki(campus)), "no wiki at all - see the previous test, which reports that as the finding.");

        const startedAt = Date.now();
        const named = await waitForOrNull(
            () => campus.api.json<WikiDetail>("get", `wikis/${campus.pin.location_slug}/`),
            (wiki) => isMeaningfulTitle(wiki.name),
            {
                what: "a wiki name matching Hudson River State Hospital / HRSH",
                timeoutMs: TITLE_WAIT_MS,
                intervalMs: 15_000,
                describe: (wiki) => `name: ${JSON.stringify(wiki.name)}`,
            },
        );
        if (named) {
            recordMetric({ name: "hrsh.wiki.seconds_to_meaningful_title", value: Math.round((Date.now() - startedAt) / 1000), unit: "s" });
        }
        expect(
            named,
            `the wiki's name never matched ${HRSH_NAME_PATTERN} within ${TITLE_WAIT_MS / 60_000} minutes. Naming comes only from ` +
                "Location/external data (WikiManager.get_or_create_for_location, tasks.enrich_wiki_location), never from the private pin - " +
                "check the account's external_apis_enabled and that a name provider answered",
        ).not.toBeNull();
        expect(leaksPrivateName(named!.name, campus), `the wiki is named ${JSON.stringify(named!.name)}, which contains the private pin's own name`).toBe(false);

        await page.goto(hrshRoutes.wiki(campus.pin.location_slug));
        const heading = ((await page.locator(".wiki-title").textContent()) ?? "").trim();
        expect(HRSH_NAME_PATTERN.test(heading), `the rendered heading reads ${JSON.stringify(heading)}, which does not name the property`).toBe(true);
        expect(leaksPrivateName(heading, campus), `the rendered heading ${JSON.stringify(heading)} contains the private pin's own name`).toBe(false);
    });

    test("the wiki's article is seeded from Wikipedia and appears, attributed, on the Article tab", async ({ campus, page }) => {
        test.skip(!(await ensureCampusWiki(campus)), "no wiki at all - see the first test in this file.");

        const startedAt = Date.now();
        const seeded = await waitForOrNull(() => campus.api.get(`wikis/${campus.pin.location_slug}/article/`), (response) => response.status() === 200, {
            what: "a seeded article on the campus wiki",
            timeoutMs: ARTICLE_WAIT_MS,
            intervalMs: 15_000,
            describe: (response) => `HTTP ${response.status()}`,
        });
        if (seeded) {
            recordMetric({ name: "hrsh.wiki.article.seconds_to_seeded", value: Math.round((Date.now() - startedAt) / 1000), unit: "s" });
        }
        expect(
            seeded,
            "no article was ever seeded from Wikipedia. seed_wiki_article_from_wikipedia resolves the wiki through getattr(location, " +
                "'wiki', None) rather than Wiki.objects.existing_for_location, so a campus wiki anchored to a different Location than the " +
                "one whose Wikipedia cache was written finds nothing (wiki_seed.py)",
        ).not.toBeNull();

        await page.goto(hrshRoutes.wiki(campus.pin.location_slug));
        await page.click('a[data-tab="article"]');
        // The panel may already have loaded with the page, so wait for its content rather than for a swap.
        await expect(page.locator("#article-panel .wiki-loading"), "the Article tab never finished loading").toHaveCount(0, { timeout: 20_000 });
        const articleText = ((await page.locator("#article-panel").innerText()) ?? "").trim();
        expect(articleText.length, "the Article tab is empty even though an article exists over the API").toBeGreaterThan(200);
        expect(/wikipedia/i.test(articleText), `the rendered article carries no Wikipedia attribution: ${JSON.stringify(articleText.slice(0, 200))}`).toBe(true);
    });

    test("the wiki's Media card fills with search-result photos with no user action", async ({ campus, page, guard }) => {
        test.skip(!(await ensureCampusWiki(campus)), "no wiki at all - see the first test in this file.");
        for (const host of THIRD_PARTY_TILE_HOSTS) {
            guard.allow(host);
        }
        await page.goto(hrshRoutes.wiki(campus.pin.location_slug));

        const tiles = await waitForOrNull(() => page.locator("#wiki-media-grid .media-item").count(), (count) => count > 0, {
            what: "at least one auto-fetched media tile on the wiki",
            timeoutMs: MEDIA_WAIT_MS,
            intervalMs: 5_000,
            describe: (count) => `${count} tile(s)`,
        });
        recordMetric({ name: "hrsh.wiki.media_tile_count", value: tiles ?? (await page.locator("#wiki-media-grid .media-item").count()), unit: "count" });

        expect(
            tiles,
            'no photo arrived on the wiki with no action beyond opening the page. Per-provider loaders fire on hx-trigger="load" - check ' +
                "external_apis_enabled and that a worker is consuming the panel_fetch queue",
        ).not.toBeNull();
    });

    test("building child pins resolve to child wikis nested under the campus wiki, meaningfully named", async ({ campus }) => {
        test.skip(!(await ensureCampusWiki(campus)), "no campus wiki at all - see the first test in this file.");
        campus.requireBoundary();
        const children = await waitForChildPins(campus, { min: 1 });
        test.skip(children.length === 0, "no child (building) pins exist yet - see hrsh-buildings.spec.ts, which reports that as its own finding.");

        const resolved = await resolveChildWikis(campus, children);
        const missing = resolved.filter((entry) => entry.wiki === null);
        const named = resolved.filter((entry) => entry.wiki !== null);

        recordMetric({ name: "hrsh.wiki.child_wiki_count", value: named.length, unit: "count", tags: { sampled: resolved.length, total_children: children.length } });
        recordMetric({ name: "hrsh.child_pins.missing_child_wiki_count", value: missing.length, unit: "count", tags: { sampled: resolved.length } });

        expect(
            missing.map((entry) => entry.pin.name),
            `${missing.length} of ${resolved.length} sampled building child pins have no wiki at their own location. mirror_buildings_to_wiki ` +
                "runs alongside create_building_pins from the same building list and swallows its own exceptions (auto_nest.py) - check the " +
                "worker log for 'mirror_buildings_to_wiki'",
        ).toEqual([]);

        const campusWiki = await campus.api.json<WikiDetail>("get", `wikis/${campus.pin.location_slug}/`);
        const badlyNamed = named.filter((entry) => !entry.wiki!.name || /^unnamed location/i.test(entry.wiki!.name) || entry.wiki!.name.toLowerCase() === campusWiki.name.toLowerCase());
        expect(
            badlyNamed.map((entry) => `${entry.pin.name} -> ${JSON.stringify(entry.wiki!.name)}`),
            "these child wikis carry a placeholder name or the parcel's own name instead of a building-appropriate one",
        ).toEqual([]);
    });

    test("the campus wiki page lists its child wikis", async ({ campus, page }) => {
        test.skip(!(await ensureCampusWiki(campus)), "no campus wiki at all - see the first test in this file.");
        campus.requireBoundary();
        const children = await waitForChildPins(campus, { min: 1 });
        test.skip(children.length === 0, "no child pins exist yet - see hrsh-buildings.spec.ts.");
        const named = (await resolveChildWikis(campus, children)).filter((entry) => entry.wiki !== null);
        test.skip(named.length === 0, "no child wiki exists yet to be listed - see the previous test, which reports that as the finding.");

        await page.goto(hrshRoutes.wiki(campus.pin.location_slug));
        await expect(
            page.locator(".pin-actions-item").filter({ hasText: "Child pin details" }),
            'the campus wiki page has no "Child pin details" toggle, which only renders when has_child_wikis is true',
        ).toBeAttached();

        // The same JSON the wiki page's own Leaflet map fetches (data-detail-pins-json-url).
        const listed = await page.request.get(`/dashboard/location/${campus.pin.location_slug}/wiki/detail-pins/json/`);
        const body = (await listed.json()) as { detail_pins?: Array<{ uuid: string }> };
        const listedUuids = new Set((body.detail_pins ?? []).map((entry) => entry.uuid));

        expect(
            named.some((entry) => listedUuids.has(entry.wiki!.uuid)),
            `none of the campus wiki's own child wikis (${named.map((entry) => entry.wiki!.name).join(", ")}) appear in its detail-pins map data (${listedUuids.size} listed)`,
        ).toBe(true);
    });

    test("a building's private pin page links to its own child wiki, not the parcel wiki", async ({ campus, page }) => {
        test.skip(!(await ensureCampusWiki(campus)), "no campus wiki at all - see the first test in this file.");
        campus.requireBoundary();
        const children = await waitForChildPins(campus, { min: 1 });
        test.skip(children.length === 0, "no child pins exist yet - see hrsh-buildings.spec.ts.");
        const resolved = await resolveChildWikis(campus, children);
        const target = resolved.find((entry) => entry.wiki !== null);
        test.skip(!target, "no child pin resolves to a wiki yet - see the child-wiki test above, which reports that as the finding.");

        await page.goto(pinDetail(target!.pin.slug));
        const hrefs = await page.locator(".pin-hero-wiki-box-link").evaluateAll((elements) => elements.map((element) => element.getAttribute("href")));

        const ownWikiPath = hrshRoutes.wiki(target!.locationSlug);
        const campusWikiPath = hrshRoutes.wiki(campus.pin.location_slug);

        expect(hrefs.length, `${target!.pin.name}'s pin page has no link to any community wiki`).toBeGreaterThan(0);
        expect(hrefs[0], `${target!.pin.name}'s most-specific wiki link is ${JSON.stringify(hrefs[0])}, not its own wiki at ${ownWikiPath}`).toBe(ownWikiPath);
        expect(hrefs[0], `${target!.pin.name} links to the parcel wiki instead of its own`).not.toBe(campusWikiPath);
    });
});
