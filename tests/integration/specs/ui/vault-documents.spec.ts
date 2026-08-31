/**
 * Vault > Documents: the parallel page to Vault Photos (Batch 5) - upload,
 * grid tile rendering with a type icon, the shared lightbox's document mode
 * (iframe preview instead of an <img>), and delete. Written against a real
 * browser per project convention ("verify behavior, not code") since the
 * icon mapping, the grid's missing-imageSelector wiring, and the lightbox's
 * image/document toggle only really prove out with an actual page load.
 */

import { expect, test } from "../../lib/fixtures.js";
import { AppShell } from "../../lib/pages/app-shell.js";
import { appRoutes } from "../../lib/routes.js";

/** A fresh tiny text file per call, so re-running against a dirty account never dedups against a prior run's upload. */
function uniqueTextFile(): { name: string; mimeType: string; buffer: Buffer } {
    return {
        name: `vault-doc-${Date.now()}-${Math.random().toString(36).slice(2)}.txt`,
        mimeType: "text/plain",
        buffer: Buffer.from(`Vault documents integration check ${Date.now()}`),
    };
}

// Serialized: every test mutates the same account's document library
// (uploads, "first tile" reads), matching vault-photos.spec.ts's reasoning
// for the same choice.
test.describe.configure({ mode: "serial" });

test.describe("vault documents page", () => {
    test("the page renders, subnav highlights Documents, and Vault Photos links back", async ({ page }) => {
        await page.goto(appRoutes.vaultDocuments);

        const shell = new AppShell(page);
        await expect(shell.nav.locator(".app-nav-link--active", { hasText: "Vault" })).toBeVisible();

        const subnav = page.locator(".vault-subnav");
        await expect(subnav.locator(".ul-subnav-tab.is-active", { hasText: "Documents" })).toBeVisible();
        await expect(subnav.locator(".ul-subnav-tab", { hasText: "Photos" })).toHaveAttribute("href", appRoutes.vaultPhotos);
    });

    test("uploading a document adds a tile with a type icon, and delete removes it", async ({ page }) => {
        await page.goto(appRoutes.vaultDocuments);

        const fileInput = page.locator("#documents-file-input");
        // The upload UI (dropzone/input) is only rendered when the account has
        // DOCUMENT_UPLOADS - test-environment SiteSettings grant it by default,
        // but skip cleanly rather than failing opaquely if that ever changes.
        test.skip((await fileInput.count()) === 0, "document uploads not enabled for this test account");

        const file = uniqueTextFile();
        await fileInput.setInputFiles(file);

        // Asserting on the tile directly (rather than #document-grid's
        // data-photo-count) also covers the empty-account case: with zero
        // prior documents there is no #document-grid to patch in place (the
        // "No documents yet" empty state renders no <ul> at all), so the
        // client reloads the whole page after this first upload - see
        // pages/vault/documents.html's _prependTile.
        const tile = page.locator(".document-tile[data-id]", { hasText: file.name });
        await expect(tile).toBeVisible({ timeout: 20000 });
        await expect(tile.locator(".document-tile-name")).toHaveText(file.name);
        // .txt maps to the "article" icon (documentIcon() / Image.document_icon).
        await expect(tile.locator(".document-tile-icon")).toHaveText("article");
        // A document tile has no thumbnail - just the icon + filename button.
        await expect(tile.locator("img")).toHaveCount(0);

        const tileId = await tile.getAttribute("data-id");
        page.once("dialog", (dialog) => dialog.accept());
        await tile.locator(".document-tile-del").click();

        await expect(page.locator(`#document-tile-${tileId}`)).toHaveCount(0);
    });

    test("the lightbox opens a document in preview mode, not image mode", async ({ page, guard }) => {
        await page.goto(appRoutes.vaultDocuments);

        // Pre-existing, unrelated to this batch (see docs/PROBLEMS.md, "A photo's
        // grid tile can 404/500 for a few seconds right after upload", addendum):
        // process_image_upload converts a document to PDF (LibreOffice) in the
        // background, replacing the just-uploaded file at its original path - an
        // iframe preview opened before that finishes briefly 404s. Not what this
        // test is about - it's asserting on the lightbox's document-mode wiring.
        guard.allow(/\/media\/pin_images\/.*vault-doc-.*\.(txt|pdf)/);

        const fileInput = page.locator("#documents-file-input");
        test.skip((await fileInput.count()) === 0, "document uploads not enabled for this test account");

        const file = uniqueTextFile();
        await fileInput.setInputFiles(file);
        const tile = page.locator(".document-tile[data-id]", { hasText: file.name });
        await expect(tile).toBeVisible({ timeout: 20000 });

        await tile.locator(".document-tile-btn").click();

        const lightbox = page.locator("#gallery-lightbox");
        await expect(lightbox).toBeVisible();
        await expect(lightbox.locator("#lightbox-document-view")).toBeVisible();
        await expect(lightbox.locator("#lightbox-document-name")).toHaveText(file.name);
        await expect(lightbox.locator("#lightbox-document-frame")).toHaveAttribute("src", /.+/);
        // The <img> path (used for photos) must stay hidden in document mode.
        await expect(lightbox.locator("#lightbox-img")).toBeHidden();

        await lightbox.locator(".lightbox-close").click();
        await expect(lightbox).toBeHidden();
    });
});
