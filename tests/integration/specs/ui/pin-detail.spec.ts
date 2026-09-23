/** The pin detail page. */

import { expect, ifSecondaryAccount, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import { PinDetailPage, type PinTab } from "../../lib/pages/pin-detail-page.js";
import { pinDetail } from "../../lib/routes.js";
import { csrfHeaders } from "../../lib/wiki.js";

const TABS: PinTab[] = ["overview", "visits", "photos", "article", "comments", "history"];

/** A 1x1 PNG with unique trailing bytes, so no rerun is deduped against an earlier upload. */
function uniquePng(marker: string): Buffer {
    const pixel = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");
    return Buffer.concat([pixel, Buffer.from(`\n${marker}`, "utf-8")]);
}

test.describe("pin detail", () => {
    test("renders the pin the API created", async ({ page, api }) => {
        const name = resourceName("detail");
        const pin = await api.createPin({ name });

        const detail = new PinDetailPage(page);
        await detail.goto(pin.slug);

        await expect(detail.hero).toContainText(name);
    });

    test("every section tab opens", async ({ page, api }) => {
        const pin = await api.createPin();
        const detail = new PinDetailPage(page);
        await detail.goto(pin.slug);

        for (const tab of TABS) {
            // A tab whose panel is missing switches the button's state and
            // shows nothing, so both halves are checked.
            await detail.openTab(tab);
            await expect(detail.content).toBeVisible();
        }
    });

    test("the Photos tab lists your photos whether or not they are in an album", async ({ page, api }) => {
        const pin = await api.createPin();
        const base = `/dashboard/map/pin/${pin.slug}`;
        const headers = await csrfHeaders(page);

        const upload = async (label: string): Promise<number> => {
            const response = await page.request.post(`${base}/gallery/`, {
                headers,
                multipart: { image: { name: `${label}.png`, mimeType: "image/png", buffer: uniquePng(resourceName(label)) } },
            });
            test.skip(response.status() === 503, "the malware scanner is unavailable, so nothing can be uploaded");
            expect(response.ok(), `uploading answered ${response.status()}`).toBeTruthy();
            return ((await response.json()) as { id: number }).id;
        };
        const loose = await upload("photos-tab-loose");
        const filed = await upload("photos-tab-filed");

        expect((await page.request.post(`${base}/albums/`, { headers, form: { name: resourceName("album") } })).ok()).toBeTruthy();
        const picker = (await (await page.request.get(`${base}/albums/?picker=1`)).json()) as { albums: { slug: string }[] };
        const albumSlug = picker.albums[0]?.slug;
        expect(albumSlug, "the album just created is not offered by the picker").toBeTruthy();
        const added = await page.request.post(`${base}/albums/${albumSlug}/add/`, { headers, data: { image_ids: [filed] } });
        expect(added.ok(), `adding to the album answered ${added.status()}`).toBeTruthy();

        const detail = new PinDetailPage(page);
        await detail.goto(pin.slug);
        await detail.openTab("photos");

        const grid = page.locator("#albums-loose-grid");
        await expect(grid.locator(`.gallery-item[data-id="${loose}"]`), "a photo in no album is missing from the Photos tab").toBeAttached();
        await expect(grid.locator(`.gallery-item[data-id="${filed}"]`), "a photo in an album is missing from the Photos tab").toBeAttached();
        await expect(page.locator("#albums-external"), "the Photos tab has no public-source section").toContainText("From public sources");
        await expect(page.locator("#albums-external .view-loading")).toHaveCount(0);

        await page.locator('[data-photos-filter="loose"]').click();
        await expect(page.locator(`#albums-loose-grid .gallery-item[data-id="${filed}"]`)).toHaveCount(0);
        await expect(page.locator(`#albums-loose-grid .gallery-item[data-id="${loose}"]`)).toBeAttached();
    });

    test("renders its own map", async ({ page, api }) => {
        const pin = await api.createPin();
        await new PinDetailPage(page).goto(pin.slug);

        // The detail map is a second Leaflet instance with different setup from
        // the main map, and has broken independently of it before.
        await expect(page.locator(".leaflet-container").first()).toBeVisible();
    });

    test("a slug that was never issued 404s", async ({ page }) => {
        const response = await page.goto(pinDetail("a-slug-that-was-never-issued-91b2c"));
        expect(response?.status()).toBe(404);
    });

    ifSecondaryAccount()("another account's pin is not reachable by guessing its URL", async ({ page, secondaryApi }) => {
        // Created by the secondary account, requested by the primary. A pin is
        // one user's private record, and the answer must be indistinguishable
        // from one for a pin that never existed - otherwise a slug becomes an
        // oracle for what other people have pinned.
        const theirs = await secondaryApi.createPin({ name: resourceName("someone else's pin") });

        const response = await page.goto(pinDetail(theirs.slug));
        expect(response?.status(), "one account could open another account's pin").toBe(404);
    });

    test("a deleted pin stops rendering", async ({ page, api }) => {
        const pin = await api.createPin();
        await new PinDetailPage(page).goto(pin.slug);
        // Leave first: the page's lazy panels would otherwise 404 against the pin mid-load.
        await page.goto("about:blank");

        await api.delete(`pins/${pin.slug}/`);

        const response = await page.goto(pinDetail(pin.slug));
        expect(response?.status(), "a deleted pin's page is still being served").toBe(404);
    });
});
