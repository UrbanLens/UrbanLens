/**
 * CRIS-backed document sources ("Sources"): automatic per-building PDFs, reachable and viewable from
 * the Article tab on both the private pin page and the community wiki page. None of this UI exists
 * yet (see scratchpad research `cris-pdfs.json`) - only the underlying CRIS fetch/cache does
 * (`plugins/builtin/cris_buildings.py`).
 *
 * UI contract:
 * - Both pages' Article tab already has (`index.html`) or must gain (`wiki.html` has no subtab strip
 *   at all today) a `.article-subtabs[role="tablist"]`. It gets a third tab: `role="tab"`, accessible
 *   name exactly "Sources", `id="article-subtab-btn-sources"`, `data-article-subtab="sources"`, wired
 *   through the existing `window.articleSetSubTab(this, 'sources')`.
 * - Its panel, `[data-article-subtab="sources"]`, holds zero or more `.article-source-item` - one per
 *   document, populated with no user action beyond selecting the tab. Each item carries
 *   `data-source-provider` (e.g. "cris_building"), `data-source-type` ("pdf"), `data-source-url` (the
 *   proxied URL streaming the real PDF bytes - never a raw REData URL), and, on a parcel/campus-scope
 *   page, `data-source-building` naming which building it documents (so distinct-building coverage is
 *   checkable without assuming one URL per building). Each item's visible title is its accessible name.
 * - Activating an item points `#article-source-viewer` (an `<iframe>`) at its `data-source-url` and
 *   reveals it - a thumbnail image does not satisfy "viewable". A same-row `.article-source-open-link`
 *   anchor (`href` = the same URL, `target="_blank"`) opens it directly too.
 */

import type { Page } from "@playwright/test";

import { ensureCampusWiki, expect, locationDataTest as test, openPrivatePin, readPin, skipUnlessLocationDataEnabled, waitForChildPins, waitForWiki } from "./fixtures.js";
import { hrshRoutes } from "../../lib/hrsh.js";
import { recordMetric, type MetricTags } from "../../lib/metrics.js";
import { waitForOrNull } from "../../lib/waiting.js";

skipUnlessLocationDataEnabled();

/** Buildings sampled per test - matches the requirement's "several buildings", kept small since each one that fails pays its full poll budget. */
const SAMPLE_SIZE = 3;

/** This reads already-cached CRIS attachments rather than fetching fresh ones, so the budget is short relative to `hrsh-media.spec.ts`'s gallery. */
const SOURCES_SETTLE_MS = 60_000;
const SOURCES_POLL_INTERVAL_MS = 5_000;

/** Same third-party map-tile hosts `hrsh-media.spec.ts` narrows; both pages carry a Leaflet map alongside the Article tab. */
const THIRD_PARTY_TILE_HOSTS = [/wayback\.maptiles\.arcgis\.com/, /tile\.openstreetmap\.org/, /server\.arcgisonline\.com/];

interface SourceItemInfo {
    provider: string | null;
    type: string | null;
    url: string | null;
    building: string | null;
}

function sourcesPanel(page: Page) {
    return page.locator('[data-article-subtab="sources"]');
}

function sourceItems(page: Page) {
    return sourcesPanel(page).locator(".article-source-item");
}

/** Clicks into the Article tab, then its Sources subtab - failing with a precise pointer at the UI contract if either is missing. */
async function openSourcesPanel(page: Page, context: string): Promise<void> {
    await page.locator('[data-tab="article"]').click();
    const tab = page.getByRole("tab", { name: "Sources" });
    await expect(
        tab,
        `${context}: no "Sources" tab exists inside the Article tab (expected role="tab", accessible name "Sources", ` +
            'id="article-subtab-btn-sources" - see this file\'s UI contract). The Article subtab strip itself may not exist on this page yet.',
    ).toBeAttached({ timeout: 15_000 });
    await tab.click();
}

/** Polls the Sources panel for its first item, recording how long that took on success. Returns the item count last seen. */
async function waitForSourcesToPopulate(page: Page, what: string, tags: MetricTags): Promise<number> {
    const startedAt = Date.now();
    const settled = await waitForOrNull(() => sourceItems(page).count(), (count) => count > 0, {
        what,
        timeoutMs: SOURCES_SETTLE_MS,
        intervalMs: SOURCES_POLL_INTERVAL_MS,
        describe: (count) => `${count} source item(s)`,
    });
    if (settled !== null) {
        recordMetric({ name: "hrsh.sources.seconds_to_populate", value: Math.round((Date.now() - startedAt) / 1000), unit: "s", tags });
    }
    return settled ?? 0;
}

async function readSourceItems(page: Page): Promise<SourceItemInfo[]> {
    const items = sourceItems(page);
    const count = await items.count();
    const out: SourceItemInfo[] = [];
    for (let index = 0; index < count; index += 1) {
        const item = items.nth(index);
        out.push({
            provider: await item.getAttribute("data-source-provider"),
            type: await item.getAttribute("data-source-type"),
            url: await item.getAttribute("data-source-url"),
            building: await item.getAttribute("data-source-building"),
        });
    }
    return out;
}

/** Activates the first Sources entry and checks it is genuinely viewable: an iframe serving real PDF bytes, plus an open-in-new-tab link. */
async function assertFirstSourceIsViewable(page: Page, context: string): Promise<void> {
    await sourceItems(page).first().click();

    const viewer = page.locator("#article-source-viewer");
    await expect(viewer, `${context}: activating the first Sources entry did not reveal #article-source-viewer`).toBeVisible({ timeout: 15_000 });

    const src = await viewer.getAttribute("src");
    expect(src, `${context}: #article-source-viewer has no src set after activating an entry`).toBeTruthy();

    const response = await page.request.get(src!);
    expect(response.ok(), `${context}: the viewer's src (${src}) answered HTTP ${response.status()}, expected 200`).toBe(true);
    const contentType = (response.headers()["content-type"] ?? "").toLowerCase();
    expect(
        contentType,
        `${context}: the viewer's src (${src}) served content-type ${JSON.stringify(contentType)}; a real PDF response ` +
            "(application/pdf) is required - a thumbnail image is not \"viewable\"",
    ).toContain("application/pdf");

    const openLink = page.locator(".article-source-open-link").first();
    await expect(openLink, `${context}: no .article-source-open-link (open in a new tab) is present next to the viewer`).toBeAttached();
    expect(await openLink.getAttribute("href"), `${context}: .article-source-open-link's href does not match the viewer's src`).toBe(src);
}

test.describe("Hudson River State Hospital - CRIS document sources", () => {
    test.beforeEach(async ({ guard }) => {
        for (const host of THIRD_PARTY_TILE_HOSTS) {
            guard.allow(host);
        }
    });

    test.describe("private pin pages", () => {
        test("at least three building child pins each show a CRIS document under Article > Sources, and it is not the same one for every building", async ({ campus, page }) => {
            const children = await waitForChildPins(campus, { min: SAMPLE_SIZE });
            expect(
                children.length,
                `only ${children.length} building child pin(s) exist under the campus pin; at least ${SAMPLE_SIZE} are needed to check ` +
                    "per-building Sources coverage. hrsh-buildings.spec.ts creates these child pins and reports their absence as its own " +
                    "finding - check that first.",
            ).toBeGreaterThanOrEqual(SAMPLE_SIZE);

            const sample = children.slice(0, SAMPLE_SIZE);
            const empty: string[] = [];
            const urls = new Set<string>();

            for (const child of sample) {
                await openPrivatePin(page, child.slug, { metricPrefix: null });
                await openSourcesPanel(page, `pin ${child.slug}`);
                const count = await waitForSourcesToPopulate(page, `a CRIS document in ${child.name}'s (${child.slug}) Sources tab`, { scope: "building_pin", slug: child.slug });
                recordMetric({ name: "hrsh.sources.pdfs_per_building", value: count, unit: "count", tags: { slug: child.slug } });
                if (count === 0) {
                    empty.push(`${child.name} (${child.slug})`);
                    continue;
                }
                for (const item of await readSourceItems(page)) {
                    if (item.url) {
                        urls.add(item.url);
                    }
                }
            }
            recordMetric({ name: "hrsh.sources.buildings_with_pdf", value: sample.length - empty.length, unit: "count" });

            expect(
                empty,
                `these building pins show no CRIS document under Article > Sources: ${JSON.stringify(empty)}. Neither the Sources tab nor ` +
                    "its backing endpoint exists yet (see this file's UI contract); CrisBuildingPanelSource already caches per-pin document " +
                    "attachments (plugins/builtin/cris_buildings.py) - the gap is exposing them here.",
            ).toEqual([]);

            expect(
                urls.size,
                `every sampled building's Sources tab pointed at the same document url(s): ${JSON.stringify([...urls])}. Per-building ` +
                    "coverage means each building's own CRIS record, not one campus-wide file reused everywhere - docs/PROBLEMS.md P24 names " +
                    "this exact failure mode.",
            ).toBeGreaterThan(1);
        });

        test("the campus (parcel) pin's Article > Sources lists CRIS documents for at least three distinct buildings", async ({ campus, page }) => {
            await openPrivatePin(page, campus.pin.slug, { metricPrefix: null });
            await openSourcesPanel(page, "the campus pin");
            const count = await waitForSourcesToPopulate(page, "a CRIS document in the campus pin's Sources tab", { scope: "campus_pin" });
            expect(count, "no CRIS document ever appeared in the campus pin's Sources tab.").toBeGreaterThan(0);

            const items = await readSourceItems(page);
            const buildings = new Set(items.map((item) => item.building).filter((value): value is string => Boolean(value)));
            recordMetric({ name: "hrsh.sources.campus_distinct_buildings", value: buildings.size, unit: "count" });

            expect(
                buildings.size,
                `the campus pin's Sources tab names ${buildings.size} distinct building(s) (${JSON.stringify([...buildings])}) across ` +
                    `${items.length} document(s). docs/PROBLEMS.md P24 records that a parcel-scope pin's CRIS lookup resolves only the ` +
                    "single nearest building; several are required here, told apart by each item's data-source-building attribute.",
            ).toBeGreaterThanOrEqual(3);
        });

        test("a Sources entry on the private pin page opens as a real, embedded PDF", async ({ campus, page }) => {
            await openPrivatePin(page, campus.pin.slug, { metricPrefix: null });
            await openSourcesPanel(page, "the campus pin");
            const count = await waitForSourcesToPopulate(page, "a CRIS document to view on the campus pin", { scope: "campus_pin_viewer" });
            expect(count, "no CRIS document appeared to view - see the campus-pin coverage test, which reports that as its own finding.").toBeGreaterThan(0);

            await assertFirstSourceIsViewable(page, "private pin page");
        });
    });

    test.describe("community wiki pages", () => {
        test("the campus wiki's Article > Sources lists CRIS documents for at least three distinct buildings", async ({ campus, page }) => {
            const ready = await ensureCampusWiki(campus);
            expect(ready, "the campus wiki never became available - hrsh-wiki.spec.ts creates it and reports that as its own finding.").toBe(true);

            await page.goto(hrshRoutes.wiki(campus.pin.location_slug));
            await openSourcesPanel(page, "the campus wiki");
            const count = await waitForSourcesToPopulate(page, "a CRIS document in the campus wiki's Sources tab", { scope: "campus_wiki" });
            expect(count, "no CRIS document ever appeared in the campus wiki's Sources tab.").toBeGreaterThan(0);

            const items = await readSourceItems(page);
            const buildings = new Set(items.map((item) => item.building).filter((value): value is string => Boolean(value)));
            recordMetric({ name: "hrsh.sources.wiki_distinct_buildings", value: buildings.size, unit: "count" });

            expect(
                buildings.size,
                `the campus wiki's Sources tab names ${buildings.size} distinct building(s) (${JSON.stringify([...buildings])}). The pin and ` +
                    "the wiki read the same per-Location cache, so this should not be harder here than on the pin page.",
            ).toBeGreaterThanOrEqual(3);
        });

        test("at least three building child wikis show a CRIS document under Article > Sources", async ({ campus, page }) => {
            const children = await waitForChildPins(campus, { min: SAMPLE_SIZE });
            expect(
                children.length,
                `only ${children.length} building child pin(s) exist; at least ${SAMPLE_SIZE} are needed to check child-wiki Sources ` +
                    "coverage. hrsh-buildings.spec.ts reports a shortfall of child pins as its own finding - check that first.",
            ).toBeGreaterThanOrEqual(SAMPLE_SIZE);

            const sample = children.slice(0, SAMPLE_SIZE);
            const findings: string[] = [];

            for (const child of sample) {
                const detail = await readPin(campus.api, child.slug);
                const wiki = await waitForWiki(campus.api, detail.location_slug, { timeoutMs: 180_000, intervalMs: 15_000 });
                if (!wiki) {
                    findings.push(`${child.name} (${child.slug}): no wiki ever appeared at wikis/${detail.location_slug}/ within 3 minutes`);
                    continue;
                }
                await page.goto(hrshRoutes.wiki(detail.location_slug));
                await openSourcesPanel(page, `wiki for ${child.name}`);
                const count = await waitForSourcesToPopulate(page, `a CRIS document in ${child.name}'s wiki Sources tab`, { scope: "building_wiki", slug: child.slug });
                if (count === 0) {
                    findings.push(`${child.name} (${child.slug}): the wiki exists but its Sources tab lists no CRIS document`);
                }
            }

            expect(
                findings,
                `${findings.length} of ${sample.length} sampled building child wikis have no CRIS document under Article > Sources:\n${findings.join("\n")}`,
            ).toEqual([]);
        });

        test("a Sources entry on the wiki page opens as a real, embedded PDF", async ({ campus, page }) => {
            const ready = await ensureCampusWiki(campus);
            expect(ready, "the campus wiki never became available - see hrsh-wiki.spec.ts.").toBe(true);

            await page.goto(hrshRoutes.wiki(campus.pin.location_slug));
            await openSourcesPanel(page, "the campus wiki");
            const count = await waitForSourcesToPopulate(page, "a CRIS document to view on the campus wiki", { scope: "campus_wiki_viewer" });
            expect(count, "no CRIS document appeared to view - see the campus-wiki coverage test, which reports that as its own finding.").toBeGreaterThan(0);

            await assertFirstSourceIsViewable(page, "wiki page");
        });
    });
});
