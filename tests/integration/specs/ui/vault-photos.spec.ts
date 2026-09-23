/** Vault > Photos: the windowed grid, off-screen pruning, sort, and upload. */

import type { Page } from "@playwright/test";

import { expect, test } from "../../lib/fixtures.js";
import { AppShell } from "../../lib/pages/app-shell.js";
import { appRoutes } from "../../lib/routes.js";

const TINY_JPEG_BASE = Buffer.from(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBD/wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCdABmX/9k=",
    "base64",
);

/** A fresh JPEG per call, so re-runs are not deduped against prior uploads by checksum. */
function uniqueTinyJpeg(): Buffer {
    return Buffer.concat([TINY_JPEG_BASE, Buffer.from(`${Date.now()}-${Math.random()}`)]);
}

/** Every media request that 404s from here on. */
function watchMissingMedia(page: Page): string[] {
    const missing: string[] = [];
    page.on("response", (response) => {
        if (response.status() === 404 && /\/media\//.test(response.url())) missing.push(response.url());
    });
    return missing;
}

async function uploadAndReadId(page: Page, name: string): Promise<number> {
    const uploaded = page.waitForResponse((response) => response.request().method() === "POST" && response.url().includes("/vault/photos/upload/"));
    await page.setInputFiles("#photos-file-input", { name, mimeType: "image/jpeg", buffer: uniqueTinyJpeg() });
    const response = await uploaded;
    expect(response.status(), await response.text()).toBe(201);
    const body = (await response.json()) as { id: number };
    return body.id;
}

/**
 * Until its re-encode lands, the tile is a labelled placeholder with no `<img>`; then it is the photo.
 * Sampled throughout, so a broken-image fallback in between fails it.
 */
async function expectPlaceholderThenPhoto(page: Page, id: number, { placeholderFirst = false } = {}): Promise<Set<string>> {
    const tile = page.locator(`#photo-tile-${id}`);
    await expect(tile).toBeVisible({ timeout: 15000 });
    const seen = new Set<string>();
    await expect
        .poll(
            async () => {
                const state = await tile.evaluate((el) => {
                    const img = el.querySelector("img");
                    if (img) return img.complete && img.naturalWidth > 0 ? "photo" : img.complete ? "broken" : "loading";
                    if (el.querySelector('.media-processing[role="img"][aria-label="Processing…"]')) return "placeholder";
                    return el.querySelector(".photo-tile-fallback") ? "fallback" : "empty";
                });
                seen.add(state);
                return state === "photo" || seen.has("broken") || seen.has("fallback") ? state : "waiting";
            },
            { timeout: 60000, intervals: [250] },
        )
        .toBe("photo");
    expect([...seen].filter((state) => !["placeholder", "loading", "photo"].includes(state)), `tile ${id} states: ${[...seen].join(", ")}`).toEqual([]);
    await expect(tile).not.toHaveAttribute("data-processing", /.*/);
    if (placeholderFirst) expect([...seen][0], "the upload response names no file, so its tile starts as a placeholder").toBe("placeholder");
    return seen;
}

// Serialized rather than left to the suite's default parallel workers: every
// test here mutates the same account's photo library (uploads, sort changes
// reading "the first tile"), and running them concurrently against shared
// server-side state produced exactly the cross-test interference this
// verification was meant to catch real bugs under, not create false ones.
test.describe.configure({ mode: "serial" });

test.describe("vault photos grid", () => {
    test("the page renders, nav highlights Vault, and the sort control is present", async ({ page }) => {
        await page.goto(appRoutes.vaultPhotos);

        const shell = new AppShell(page);
        await expect(shell.nav.locator("a.app-nav-link", { hasText: "Vault" })).toHaveClass(/app-nav-link/);
        // Scoped to the desktop nav specifically - the mobile drawer duplicates
        // every link (including the active one) elsewhere in the DOM.
        await expect(shell.nav.locator(".app-nav-link--active", { hasText: "Vault" })).toBeVisible();

        const sort = page.locator("#vault-photos-sort");
        await expect(sort).toBeVisible();
        const values = await sort.locator("option").evaluateAll((opts) => opts.map((o) => (o as HTMLOptionElement).value));
        expect(values).toEqual(expect.arrayContaining(["recent", "oldest", "taken", "name"]));
        await expect(sort).toHaveValue("recent");
    });

    test("scrolling loads further pages and prunes off-screen thumbnails", async ({ page }) => {
        // Narrow rather than default-wide: the grid is `repeat(auto-fill,
        // minmax(96px, 1fr))`, so a wide viewport packs the same 100+ seeded
        // photos into few, short rows - not tall enough, even scrolled to the
        // page's true bottom, to push the first tile 1200px (the pruning
        // buffer) past the viewport. A narrow one guarantees enough rows.
        await page.setViewportSize({ width: 380, height: 700 });
        await page.goto(appRoutes.vaultPhotos);

        const grid = page.locator("#photo-grid");
        await expect(grid).toBeVisible();
        const initialCount = await grid.locator(".photo-tile[data-id]").count();
        expect(initialCount, "the first page should render some tiles").toBeGreaterThan(0);

        const total = Number.parseInt((await grid.getAttribute("data-photo-count")) ?? "0", 10);
        // Only meaningful with more than one page's worth of photos.
        test.skip(total <= initialCount, "not enough seeded photos to exercise pagination");

        const firstImg = grid.locator(".photo-tile[data-id] img").first();
        await expect(firstImg).toHaveAttribute("src", /.+/);

        // Scroll to the true bottom of the page (not a fixed pixel guess),
        // triggering further fetches along the way.
        await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
        await page.waitForTimeout(200);
        await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
        await expect
            .poll(async () => grid.locator(".photo-tile[data-id]").count(), { timeout: 10000 })
            .toBeGreaterThan(initialCount);
        await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
        await page.waitForTimeout(300);

        // At least one of the first several tiles should have been pruned
        // (its <img src> cleared, moved to data-src) now that it's far above
        // the viewport - not necessarily the very first, since layout/seed
        // data can shift exactly where the 1200px buffer line falls.
        const diagnostics = await page.evaluate(() => {
            const imgs = Array.from(document.querySelectorAll<HTMLImageElement>(".photo-tile[data-id] img")).slice(0, 12);
            return {
                scrollY: window.scrollY,
                pageHeight: document.documentElement.scrollHeight,
                viewportHeight: window.innerHeight,
                tiles: imgs.map((img) => ({ hasSrc: Boolean(img.getAttribute("src")), hasDataSrc: Boolean(img.dataset.src), top: img.getBoundingClientRect().top, bottom: img.getBoundingClientRect().bottom })),
            };
        });
        // The grid prunes past UNLOAD_BUFFER_PX (photo-virtual-grid.ts); an account whose whole grid is shorter has nothing to prune.
        test.skip(Math.min(...diagnostics.tiles.map((t) => t.bottom)) > -1200, "not enough photos to scroll a tile past the prune buffer");
        const prunedCount = diagnostics.tiles.filter((t) => !t.hasSrc && t.hasDataSrc).length;
        expect(prunedCount, `expected at least one early tile pruned after scrolling to the bottom. Diagnostics: ${JSON.stringify(diagnostics)}`).toBeGreaterThan(0);

        // Scroll back to the top; the pruned image(s) should be restored.
        await page.evaluate(() => window.scrollTo(0, 0));
        await page.waitForTimeout(300);
        await expect(firstImg).toHaveAttribute("src", /.+/);
    });

    test("changing sort re-fetches the grid in the new order", async ({ page }) => {
        await page.goto(appRoutes.vaultPhotos);

        const grid = page.locator("#photo-grid");
        await expect(grid).toBeVisible();

        // Compare first pages across sorts without depending on captions, filenames, or upload position (all order-dependent).
        const idsUnder = async (sort: string): Promise<(string | undefined)[]> => {
            await page.locator("#vault-photos-sort").selectOption(sort);
            // The sort handler clears the grid and re-fetches from scratch.
            await expect.poll(async () => grid.locator(".photo-tile[data-id]").count(), { timeout: 10000 }).toBeGreaterThan(0);
            return grid.locator(".photo-tile[data-id]").evaluateAll((els) => els.map((el) => (el as HTMLElement).dataset.id));
        };

        const byRecent = await idsUnder("recent");
        expect(byRecent.length, "a library of one photo cannot show a sort changing anything").toBeGreaterThan(1);

        const byName = await idsUnder("name");
        expect(byName, "changing the sort re-fetched the same page in the same order").not.toEqual(byRecent);

        // Where the two pages overlap, the shared photos must appear in a
        // different relative order - a stronger statement than "the lists
        // differ", which a changed page *size* would also satisfy.
        const shared = byRecent.filter((id) => byName.includes(id));
        if (shared.length > 1) {
            const reordered = [...shared].sort((a, b) => byName.indexOf(a) - byName.indexOf(b));
            expect(reordered, `the ${shared.length} shared photos kept their relative order across sorts`).not.toEqual(shared);
        }
    });

    test("a photo uploaded while sorted by name doesn't duplicate or corrupt the grid", async ({ page }) => {
        await page.goto(appRoutes.vaultPhotos);

        const grid = page.locator("#photo-grid");
        await expect(grid).toBeVisible();
        const totalBefore = Number.parseInt((await grid.getAttribute("data-photo-count")) ?? "0", 10);

        await page.locator("#vault-photos-sort").selectOption("name");
        await expect.poll(async () => grid.locator(".photo-tile[data-id]").count(), { timeout: 10000 }).toBeGreaterThan(0);

        const missingMedia = watchMissingMedia(page);
        const id = await uploadAndReadId(page, "aaa-uploaded-during-name-sort.jpg");

        // The upload completes, then _finishUpload re-fetches the grid under
        // the active (non-recent) sort - see pages/vault/photos.html.
        await expect.poll(async () => Number.parseInt((await grid.getAttribute("data-photo-count")) ?? "0", 10), { timeout: 15000 }).toBe(totalBefore + 1);

        // No duplicate tiles: every data-id on the page is unique.
        const ids = await grid.locator(".photo-tile[data-id]").evaluateAll((els) => els.map((el) => (el as HTMLElement).dataset.id));
        expect(new Set(ids).size, `duplicate tiles after upload: ${ids.join(",")}`).toBe(ids.length);

        // Name sort orders by caption, so a captionless upload lands among the other untitled photos by id and
        // may fall past the first page; when it is rendered, it must show the placeholder, never a broken file.
        if ((await page.locator(`#photo-tile-${id}`).count()) > 0) await expectPlaceholderThenPhoto(page, id);
        expect(missingMedia, "a tile requested a media file that was not there").toEqual([]);
    });

    test("a fresh upload shows a processing placeholder, then its photo", async ({ page }) => {
        await page.goto(appRoutes.vaultPhotos);
        await expect(page.locator("#vault-photos-sort")).toHaveValue("recent");

        const missingMedia = watchMissingMedia(page);
        const id = await uploadAndReadId(page, "fresh-upload-placeholder.jpg");

        const tile = page.locator(`#photo-tile-${id}`);
        await expectPlaceholderThenPhoto(page, id, { placeholderFirst: true });
        await expect(tile.locator(".photo-tile-btn")).toBeEnabled();
        expect(missingMedia, "a tile requested a media file that was not there").toEqual([]);
    });

    test("the lightbox opens from a grid tile", async ({ page }) => {
        await page.goto(appRoutes.vaultPhotos);

        const grid = page.locator("#photo-grid");
        await grid.locator(".photo-tile-btn").first().click();

        const lightbox = page.locator("#gallery-lightbox");
        await expect(lightbox).toBeVisible();
        await expect(lightbox.locator("#lightbox-img")).toHaveAttribute("src", /.+/);
    });
});
