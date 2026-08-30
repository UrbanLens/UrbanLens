/**
 * Vault > Photos: the windowed grid, off-screen pruning, sort, and upload.
 *
 * Ad hoc verification for the Vault feature's Batch 2 (performance/sort),
 * written against a real browser rather than trusted from the TS unit suite
 * alone - the windowing/pruning/sort-aware-upload logic only really exists
 * once IntersectionObserver, real scrolling, and a real fetch loop are in
 * play, none of which the unit tests exercise.
 */

import { expect, test } from "../../lib/fixtures.js";
import { AppShell } from "../../lib/pages/app-shell.js";
import { appRoutes } from "../../lib/routes.js";

const TINY_JPEG_BASE = Buffer.from(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBD/wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCdABmX/9k=",
    "base64",
);

/**
 * A fresh JPEG for each call, so a re-run of this spec against an account
 * that already has the previous run's upload doesn't get silently treated as
 * a duplicate by the server's per-profile checksum dedup - trailing bytes
 * after the JPEG's EOI marker are ignored by every decoder that matters here
 * but change the file's hash.
 */
function uniqueTinyJpeg(): Buffer {
    return Buffer.concat([TINY_JPEG_BASE, Buffer.from(`${Date.now()}-${Math.random()}`)]);
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
                tiles: imgs.map((img) => ({ hasSrc: Boolean(img.getAttribute("src")), hasDataSrc: Boolean(img.dataset.src), top: img.getBoundingClientRect().top })),
            };
        });
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
        const firstTileIdBefore = await grid.locator(".photo-tile[data-id]").first().getAttribute("data-id");

        await page.locator("#vault-photos-sort").selectOption("name");
        // The sort handler clears the grid and re-fetches from scratch.
        await expect.poll(async () => grid.locator(".photo-tile[data-id]").count(), { timeout: 10000 }).toBeGreaterThan(0);

        const firstTileIdAfter = await grid.locator(".photo-tile[data-id]").first().getAttribute("data-id");
        // With 30+ randomly captioned seed photos, name order should not
        // coincidentally match recent-upload order.
        expect(firstTileIdAfter).not.toBe(firstTileIdBefore);
    });

    test("a photo uploaded while sorted by name doesn't duplicate or corrupt the grid", async ({ page }) => {
        await page.goto(appRoutes.vaultPhotos);

        const grid = page.locator("#photo-grid");
        await expect(grid).toBeVisible();
        const totalBefore = Number.parseInt((await grid.getAttribute("data-photo-count")) ?? "0", 10);

        await page.locator("#vault-photos-sort").selectOption("name");
        await expect.poll(async () => grid.locator(".photo-tile[data-id]").count(), { timeout: 10000 }).toBeGreaterThan(0);

        await page.setInputFiles("#photos-file-input", {
            name: "aaa-uploaded-during-name-sort.jpg",
            mimeType: "image/jpeg",
            buffer: uniqueTinyJpeg(),
        });

        // The upload completes, then _finishUpload re-fetches the grid under
        // the active (non-recent) sort - see pages/vault/photos.html.
        await expect.poll(async () => Number.parseInt((await grid.getAttribute("data-photo-count")) ?? "0", 10), { timeout: 15000 }).toBe(totalBefore + 1);

        // No duplicate tiles: every data-id on the page is unique.
        const ids = await grid.locator(".photo-tile[data-id]").evaluateAll((els) => els.map((el) => (el as HTMLElement).dataset.id));
        expect(new Set(ids).size, `duplicate tiles after upload: ${ids.join(",")}`).toBe(ids.length);
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
