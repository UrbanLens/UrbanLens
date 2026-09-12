/**
 * External media arriving on the pin detail page and on the wiki. The media gallery is entirely
 * lazy.
 */

import { expect, locationDataTest as test, skipUnlessLocationDataEnabled } from "./fixtures.js";
import { hrshRoutes } from "../../lib/hrsh.js";

skipUnlessLocationDataEnabled();

/** The gallery's own poll budget, plus room for the task behind it. */
const GALLERY_SETTLE_MS = 120_000;

/** Gap between observations while waiting for the tile count to stop moving. */
const GALLERY_POLL_MS = 2_500;

/** Consecutive unchanged observations that count as "finished". */
const GALLERY_STABLE_POLLS = 4;

/**
 * Third-party origins whose failures are not this spec's subject.
 *
 * Measured on the pin detail page: `wayback.maptiles.arcgis.com` is blocked by
 * Chrome's Opaque Response Blocking and an OpenStreetMap tile aborts. Those are
 * the *map* layers, not gallery items - but the console guard fails any test
 * whose page logged a failed subresource, so without narrowing it every media
 * test dies at ~8 s on a page whose gallery is working perfectly.
 *
 * Whether third-party origins are reachable is a real question, and
 * `specs/services/third-party-origins.spec.ts` asks it deliberately. Asking it
 * again here, by accident, only obscures what these tests are for.
 */
const THIRD_PARTY_TILE_HOSTS = [/wayback\.maptiles\.arcgis\.com/, /tile\.openstreetmap\.org/, /server\.arcgisonline\.com/];

/**
 * Waits for a media gallery to stop filling, and returns how many tiles it has.
 *
 * **The two pages need different treatment, and using one signal for both was a
 * real defect in this file.** The pin page removes each provider loader as it
 * completes (`el.remove()`), so counting `.media-provider-loader` down to zero
 * works there. The wiki page never removes them: measured, it sits at 13 loaders
 * indefinitely while its tiles arrive within seconds, and `#wiki-media-loading`
 * stays visible too. Counting to zero there can only ever time out - which is
 * exactly what happened, for two minutes per test, on a gallery that had already
 * finished.
 *
 * So the signal used here is the one both pages share: the tile count stops
 * changing. Zero loaders is kept as a fast path for the pin page, because when
 * it is available it is unambiguous.
 *
 * @param page The page showing a media gallery.
 * @returns How many media tiles the grid settled on.
 */
async function settleGallery(page: import("@playwright/test").Page): Promise<number> {
    const deadline = Date.now() + GALLERY_SETTLE_MS;
    let previous = -1;
    let unchanged = 0;

    while (Date.now() < deadline) {
        const tiles = await page.locator(".media-item").count();
        if (tiles === previous) {
            unchanged += 1;
        } else {
            unchanged = 0;
            previous = tiles;
        }
        // The pin page's own completion signal, when it is offered.
        if ((await page.locator(".media-provider-loader").count()) === 0) {
            return tiles;
        }
        if (unchanged >= GALLERY_STABLE_POLLS) {
            return tiles;
        }
        await page.waitForTimeout(GALLERY_POLL_MS);
    }
    return page.locator(".media-item").count();
}

test.describe("Hudson River State Hospital - external media", () => {
    // The pin detail page carries satellite and street map layers alongside the
    // gallery, and those tiles come from origins that fail in this environment.
    // See THIRD_PARTY_TILE_HOSTS for why that is narrowed rather than tolerated
    // wholesale.
    test.beforeEach(async ({ guard }) => {
        for (const host of THIRD_PARTY_TILE_HOSTS) {
            guard.allow(host);
        }
    });

    test("the pin detail page has a media gallery section", async ({ campus, page }) => {
        await page.goto(`/dashboard/map/pin/${campus.pin.slug}/`);

        await expect(
            page.locator("#media-gallery-section"),
            "the pin detail page has no media gallery section at all, which is a template problem rather than a data one",
        ).toBeAttached();
    });

    test("external media items arrive on the pin detail page", async ({ campus, page }) => {
        await page.goto(`/dashboard/map/pin/${campus.pin.slug}/`);
        const tiles = await settleGallery(page);

        expect(
            tiles,
            "the gallery finished loading with no items. For a site with a Wikipedia article, a Library of Congress presence and a CRIS " +
                "record, zero external media means the providers were not reached rather than that there is nothing to show. Check the " +
                "account's external_apis_enabled first - provisioned accounts have it off unless --external-apis was passed",
        ).toBeGreaterThan(0);
    });

    test("media tiles say where they came from", async ({ campus, page }) => {
        await page.goto(`/dashboard/map/pin/${campus.pin.slug}/`);
        const tiles = await settleGallery(page);
        test.skip(tiles === 0, "no media arrived - see the previous test, which reports that as the finding.");

        // Attribution is not decoration here: these are third-party images shown
        // under someone else's terms, and a tile with no source is a licensing
        // problem as much as a UI one.
        const attributed = await page.locator(".media-item [data-provider], .media-item .media-item-source").count();

        expect(attributed, `${tiles} media tiles rendered but none carries a provider attribution`).toBeGreaterThan(0);
    });

    test("the wiki page shows media for the same place", async ({ campus, page }) => {
        // The wiki has to exist first; the promotion path is a browser POST.
        const wiki = await campus.api.get(`wikis/${campus.pin.location_slug}/`);
        test.skip(wiki.status() !== 200, "no wiki for the campus yet - hrsh-wiki.spec.ts creates it and reports if that fails.");

        await page.goto(hrshRoutes.wiki(campus.pin.location_slug));
        await expect(page.locator("#wiki-media-section"), "the wiki page has no media section").toBeAttached();

        const tiles = await settleGallery(page);
        expect(
            tiles,
            "the wiki's media gallery finished with no items. The wiki and the pin draw from the same per-Location cache rows, so if the " +
                "pin page has media and this does not, the two galleries disagree about the same data rather than the data being absent",
        ).toBeGreaterThan(0);
    });

    test("the pin and the wiki agree about how much media the place has", async ({ campus, page }) => {
        const wiki = await campus.api.get(`wikis/${campus.pin.location_slug}/`);
        test.skip(wiki.status() !== 200, "no wiki for the campus yet.");

        await page.goto(`/dashboard/map/pin/${campus.pin.slug}/`);
        const onPin = await settleGallery(page);

        await page.goto(hrshRoutes.wiki(campus.pin.location_slug));
        const onWiki = await settleGallery(page);

        test.skip(onPin === 0 && onWiki === 0, "neither surface has media, so there is nothing to compare.");

        // Not equality: the two pages carry different loader sets, and two media
        // sources are registered but have no loader div on either template
        // (chronicling_america and redata_aerial), so a difference is expected.
        // A total absence on one side while the other is populated is not.
        expect(
            onPin > 0 && onWiki > 0,
            `the pin page shows ${onPin} media items and the wiki shows ${onWiki}. Both read the same per-Location cache, so one being ` +
                "empty while the other is populated points at that page's loader set rather than at the data",
        ).toBe(true);
    });

    test("a pending gallery is visibly pending rather than silently empty", async ({ campus, page }) => {
        // The failure mode this guards is a user-facing one: a gallery that is
        // still fetching must not look like a gallery that found nothing.
        await page.goto(`/dashboard/map/pin/${campus.pin.slug}/`);

        const loaders = await page.locator(".media-provider-loader").count();
        test.skip(loaders === 0, "the gallery was already warm on arrival, so there was no pending state to observe.");

        await expect(
            page.locator("#media-gallery-loading"),
            "provider loaders are still in flight but nothing on the page says the gallery is still filling, so it reads as empty",
        ).toBeAttached();
    });
});
