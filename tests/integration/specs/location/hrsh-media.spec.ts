/**
 * External media arriving on the pin detail page and on the wiki.
 *
 * ## Nothing is fetched until somebody looks
 *
 * The media gallery is entirely lazy. Creating a pin fetches no imagery at all;
 * the first page view finds no fresh `LocationCache` row, calls
 * `schedule_panel_fetch`, and returns a self-polling placeholder carrying
 * `UL-Panel-Pending: 1`. That placeholder re-polls every 2 s up to 30 times
 * (~60 s) while a `fetch_panel_source` task runs on the **`panel_fetch`** queue.
 *
 * The queue matters more than it looks: the default Celery worker does not
 * consume `panel_fetch`. A deployment running only `celery-worker` and not
 * `celery-worker-panels` will show every gallery pending forever, and nothing
 * in the UI says so. That is worth ruling out before reading a failure here as
 * a data problem.
 *
 * ## Why `waitForHtmxSettled` is wrong here
 *
 * The pending loaders poll with `hx-trigger="load delay:2s"`, so there are ~2 s
 * windows in which no HTMX request is in flight and the gallery is still
 * mid-fetch. A settle-based wait passes straight through them and asserts on an
 * empty grid. The signal that actually means "finished" differs by page:
 *
 * - **Pin page:** each loader removes itself (`el.remove()`), and
 *   `#media-gallery-loading` is removed from the DOM.
 * - **Wiki page:** loaders are never removed; `#wiki-media-loading` is only
 *   hidden. There is also a 15 s timer that reveals `#wiki-media-empty`, which a
 *   provider landing at t+40 s then hides again - so "empty is visible" is not a
 *   stable conclusion until well past the poll budget.
 *
 * Both are handled by counting `.media-provider-loader` down to zero rather than
 * trusting either page's own completion flag.
 */

import { expect, locationDataTest as test, skipUnlessLocationDataEnabled } from "./fixtures.js";
import { hrshRoutes } from "../../lib/hrsh.js";

skipUnlessLocationDataEnabled();

/** The gallery's own poll budget, plus room for the task behind it. */
const GALLERY_SETTLE_MS = 120_000;

/**
 * Waits for every provider loader on the current page to finish.
 *
 * @param page The page showing a media gallery.
 * @returns How many media tiles ended up in the grid.
 */
async function settleGallery(page: import("@playwright/test").Page): Promise<number> {
    await expect
        .poll(async () => page.locator(".media-provider-loader").count(), {
            timeout: GALLERY_SETTLE_MS,
            message:
                "media provider loaders never finished. Each polls every 2s for up to 30 attempts while a fetch_panel_source task runs " +
                "on the panel_fetch queue - if no worker consumes that queue they poll out and stay pending, which looks identical to " +
                "slow providers",
        })
        .toBe(0);
    return page.locator(".media-item").count();
}

test.describe("Hudson River State Hospital - external media", () => {
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
