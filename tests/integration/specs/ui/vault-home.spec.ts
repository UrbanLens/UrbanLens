/**
 * Vault home (Batch 6): the `/vault/` landing page - counts/quick-links into
 * Photos and Documents, the storage usage bar, and the recent-uploads strip.
 * Written against a real browser per project convention, since the counts
 * and links are exactly the kind of thing that can silently drift from the
 * pages they're supposed to summarize.
 */

import { expect, test } from "../../lib/fixtures.js";
import { AppShell } from "../../lib/pages/app-shell.js";
import { appRoutes } from "../../lib/routes.js";

test.describe("vault home page", () => {
    test("the page renders, nav highlights Vault, and subnav highlights the Vault tab", async ({ page }) => {
        await page.goto(appRoutes.vaultHome);

        const shell = new AppShell(page);
        await expect(shell.nav.locator(".app-nav-link--active", { hasText: "Vault" })).toBeVisible();

        const subnav = page.locator(".vault-subnav");
        await expect(subnav.locator(".ul-subnav-tab.is-active", { hasText: "Vault" })).toBeVisible();
    });

    test("the Photos and Documents quick-link tiles show live counts and link to their pages", async ({ page }) => {
        await page.goto(appRoutes.vaultHome);

        const photosTile = page.locator(`.vault-stat-tile[href="${appRoutes.vaultPhotos}"]`);
        await expect(photosTile).toBeVisible();
        const photosCountOnHome = await photosTile.locator(".vault-stat-value").innerText();

        const documentsTile = page.locator(`.vault-stat-tile[href="${appRoutes.vaultDocuments}"]`);
        await expect(documentsTile).toBeVisible();

        // Cross-check the Photos tile's count against the Photos page's own
        // grid count, rather than just asserting it renders a number - a
        // stale/miscomputed count wouldn't be caught by presence alone.
        await page.goto(appRoutes.vaultPhotos);
        const photoGridCount = await page.locator("#photo-grid").getAttribute("data-photo-count");
        expect(photosCountOnHome).toBe(photoGridCount ?? "0");
    });

    test("the storage usage bar renders a used/quota summary", async ({ page }) => {
        await page.goto(appRoutes.vaultHome);

        const storage = page.locator(".vault-home-storage");
        await expect(storage).toBeVisible();
        await expect(storage.locator(".storage-usage__text")).toContainText("used");
    });

    // The secondary account is the suite's "no content of its own" account, so
    // it exercises the brand-new-user path the primary account can no longer
    // reach. Three zeroed stat tiles and an empty storage bar tell a new user
    // nothing, so the page swaps them for a welcome + the two ways in.
    test("a vault with nothing in it shows a welcome and a way in, not zeroed tiles", async ({ secondaryPage }) => {
        await secondaryPage.goto(appRoutes.vaultHome);

        const empty = secondaryPage.locator(".memories-empty-state");
        await expect(empty).toBeVisible();
        await expect(empty.locator("h2")).toHaveText("Your Vault is empty");
        await expect(empty.locator(`a[href="${appRoutes.vaultPhotos}"]`)).toBeVisible();
        await expect(empty.locator(`a[href="${appRoutes.vaultDocuments}"]`)).toBeVisible();

        // The stat tiles and storage bar are replaced by it, not shown alongside.
        await expect(secondaryPage.locator(".vault-home-stats")).toHaveCount(0);
        await expect(secondaryPage.locator(".vault-home-storage")).toHaveCount(0);
    });
});
