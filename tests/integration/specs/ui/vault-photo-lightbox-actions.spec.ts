/**
 * The shared lightbox's Batch 4 additions, verified from Vault > Photos: the
 * pin/wiki association panel and the "File to a pin" picker. Written against
 * a real browser (not just the unit suite) since the whole point of this
 * batch is a shared <dialog>, debounced search, and an AJAX-refreshed panel
 * all driven by vanilla JS - exactly the kind of wiring that looks correct
 * from a source read and isn't.
 */

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import { appRoutes } from "../../lib/routes.js";

const TINY_JPEG_BASE = Buffer.from(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBD/wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCdABmX/9k=",
    "base64",
);

function uniqueTinyJpeg(): Buffer {
    return Buffer.concat([TINY_JPEG_BASE, Buffer.from(`${Date.now()}-${Math.random()}`)]);
}

test.describe.configure({ mode: "serial" });

test.describe("lightbox pin association and file-to-pin picker", () => {
    test("an unfiled photo's lightbox offers File to a pin; picking one updates the panel", async ({ page, api, guard }) => {
        // Pre-existing, unrelated to this batch (see docs/PROBLEMS.md, "A photo's
        // grid tile can 404/500 for a few seconds right after upload"): the async
        // upload-processing task can re-encode and rename the stored file, and a
        // tile rendered before (or racing) that rename briefly 404s/500s. Not
        // what this test is about - it's asserting on the lightbox's associations
        // panel and picker, not on upload-processing timing.
        guard.allow(/\/media\/pin_images\/.*lightbox-associations\.(jpg|webp)/);

        const pinName = resourceName("E2E lightbox target pin");
        await api.createPin({ name: pinName });

        await page.goto(appRoutes.vaultPhotos);
        const grid = page.locator("#photo-grid");
        await expect(grid).toBeVisible();

        const totalBefore = Number.parseInt((await grid.getAttribute("data-photo-count")) ?? "0", 10);
        await page.setInputFiles("#photos-file-input", {
            name: "lightbox-associations.jpg",
            mimeType: "image/jpeg",
            buffer: uniqueTinyJpeg(),
        });
        await expect.poll(async () => Number.parseInt((await grid.getAttribute("data-photo-count")) ?? "0", 10), { timeout: 15000 }).toBe(totalBefore + 1);

        // Default sort is recent-uploads-first, so the new photo is the first tile.
        await grid.locator(".photo-tile-btn").first().click();
        const lightbox = page.locator("#gallery-lightbox");
        await expect(lightbox).toBeVisible();

        const associations = lightbox.locator("#lightbox-associations");
        await expect(associations.locator("button", { hasText: "File to a pin" })).toBeVisible({ timeout: 10000 });
        await expect(associations.locator("button", { hasText: "Send to a wiki" })).toBeVisible();

        await associations.locator("button", { hasText: "File to a pin" }).click();
        const picker = page.locator("#lightbox-picker-dialog");
        await expect(picker).toBeVisible();
        await expect(picker.locator("#lightbox-picker-title")).toHaveText("File to a pin");

        await picker.locator("#lightbox-picker-search").fill(pinName);
        const result = picker.locator(".photo-pin-result", { hasText: pinName });
        await expect(result).toBeVisible({ timeout: 10000 });
        await result.click();

        // The shared picker dialog closes on success, and the associations
        // panel (loaded fresh via _loadLightboxAssociations) now shows the
        // pin instead of the "File to a pin" trigger.
        await expect(picker).toBeHidden();
        await expect(associations.locator("a", { hasText: pinName })).toBeVisible({ timeout: 10000 });
        await expect(associations.locator("button", { hasText: "File to a pin" })).toHaveCount(0);
    });

    test("the share action is offered from the actions menu on the viewer's own photo", async ({ page }) => {
        await page.goto(appRoutes.vaultPhotos);
        const grid = page.locator("#photo-grid");
        await expect(grid.locator(".photo-tile-btn").first()).toBeVisible({ timeout: 10000 });
        await grid.locator(".photo-tile-btn").first().click();

        const lightbox = page.locator("#gallery-lightbox");
        await expect(lightbox).toBeVisible();
        const menuToggle = lightbox.locator("#lightbox-actions-toggle");
        await expect(menuToggle).toBeVisible({ timeout: 10000 });
        await menuToggle.click();
        await expect(lightbox.locator("#lightbox-share-action")).toBeVisible();
    });
});
