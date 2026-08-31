/**
 * Vault > Photos albums: the Vault's own album panel (lazily loaded), and the
 * "Show your pin albums" toggle - a cross-pin, read-only discovery listing.
 */

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import { PinDetailPage } from "../../lib/pages/pin-detail-page.js";
import { appRoutes } from "../../lib/routes.js";

test.describe.configure({ mode: "serial" });

// Unique per run (see lib/env.js's resourceName) - this dev environment's
// database persists across runs, so a fixed name would collide with an
// earlier run's leftover album and break the later tests' single-match
// locators instead of testing anything.
const ALBUM_NAME = resourceName("E2E vault album");

test.describe("vault albums", () => {
    test("the vault's own album panel loads and a new album can be created", async ({ page }) => {
        await page.goto(appRoutes.vaultPhotos);

        const panel = page.locator("#vault-albums-panel");
        await expect(panel.locator("#albums-panel")).toBeVisible();

        await panel.locator('[data-album-create-toggle]').click();
        const nameInput = panel.locator(".album-create-name");
        await expect(nameInput).toBeVisible();
        await nameInput.fill(ALBUM_NAME);
        await panel.locator('.album-create-form button[type="submit"]').click();

        await expect(page.locator(".album-card-name", { hasText: ALBUM_NAME })).toBeVisible();
    });

    test("the pin-albums toggle lazily reveals albums from across the profile's pins", async ({ page, api }) => {
        // This account's own pin + album, created fresh by this test rather
        // than assumed to pre-exist - a previous version of this test relied
        // on data left over from manual testing in one specific dev DB, which
        // silently passed there and would time out on any fresh environment.
        const pinName = resourceName("E2E test place");
        const pinAlbumName = resourceName("E2E pin album");
        const pin = await api.createPin({ name: pinName });

        const detail = new PinDetailPage(page);
        await detail.goto(pin.slug);
        await detail.openTab("photos");

        const pinAlbumsSection = page.locator("#albums-panel");
        await expect(pinAlbumsSection).toBeVisible();
        await pinAlbumsSection.locator("[data-album-create-toggle]").click();
        await pinAlbumsSection.locator(".album-create-name").fill(pinAlbumName);
        await pinAlbumsSection.locator('.album-create-form button[type="submit"]').click();
        await expect(page.locator(".album-card-name", { hasText: pinAlbumName })).toBeVisible();

        await page.goto(appRoutes.vaultPhotos);

        const details = page.locator(".vault-pin-albums-toggle-wrap");
        const pinAlbumsPanel = page.locator("#vault-pin-albums-panel");

        // Closed by default - no fetch has happened yet.
        await expect(details).not.toHaveAttribute("open", "");

        await details.locator("summary").click();
        await expect(details).toHaveAttribute("open", "");
        await expect(pinAlbumsPanel.locator(".album-card-name", { hasText: pinAlbumName })).toBeVisible({ timeout: 10000 });
        await expect(pinAlbumsPanel.locator(".album-card-pin")).toContainText(pinName);

        // Closing and reopening doesn't refetch (hx-trigger="toggle once") -
        // assert on the actual request count, not just on DOM state that a
        // refetch of identical fixture data would reproduce indistinguishably.
        let fetchCount = 0;
        await page.route("**/vault/photos/pin-albums/**", async (route) => {
            fetchCount += 1;
            await route.continue();
        });
        await details.locator("summary").click();
        await expect(details).not.toHaveAttribute("open", "");
        await details.locator("summary").click();
        await expect(pinAlbumsPanel.locator(".album-card-name", { hasText: pinAlbumName })).toBeVisible();
        expect(fetchCount).toBe(0);
    });

    test("clicking a pin album card in the toggle lands on that pin's own Photos tab", async ({ page, api }) => {
        const pinName = resourceName("E2E pin album link target");
        const pinAlbumName = resourceName("E2E pin album link");
        const pin = await api.createPin({ name: pinName });

        const detail = new PinDetailPage(page);
        await detail.goto(pin.slug);
        await detail.openTab("photos");
        const pinAlbumsSection = page.locator("#albums-panel");
        await pinAlbumsSection.locator("[data-album-create-toggle]").click();
        await pinAlbumsSection.locator(".album-create-name").fill(pinAlbumName);
        await pinAlbumsSection.locator('.album-create-form button[type="submit"]').click();
        await expect(page.locator(".album-card-name", { hasText: pinAlbumName })).toBeVisible();

        await page.goto(appRoutes.vaultPhotos);
        const details = page.locator(".vault-pin-albums-toggle-wrap");
        const pinAlbumsPanel = page.locator("#vault-pin-albums-panel");
        await details.locator("summary").click();
        const card = pinAlbumsPanel.locator(".album-card-name", { hasText: pinAlbumName });
        await expect(card).toBeVisible({ timeout: 10000 });

        // A real, chrome-having navigation - not the bare AJAX detail partial.
        await card.click();
        await detail.expectLoaded();
        await expect(detail.tab("photos")).toHaveClass(/is-active/);
        await expect(page.locator("#albums-panel .album-detail-title", { hasText: pinAlbumName })).toBeVisible();
    });

    test("opening a vault album swaps in its own detail view with add/upload controls", async ({ page }) => {
        await page.goto(appRoutes.vaultPhotos);

        const panel = page.locator("#vault-albums-panel");
        await expect(panel.locator(".album-card-name", { hasText: ALBUM_NAME })).toBeVisible();
        await panel.locator(".album-card-name", { hasText: ALBUM_NAME }).click();

        // #albums-panel is swapped wholesale into its single-album form -
        // data-album-slug and the "All albums" back button only exist there.
        const detail = page.locator("#albums-panel.album-detail");
        await expect(detail).toHaveAttribute("data-album-slug", /.+/);
        await expect(detail).toHaveAttribute("data-add-url", /.+/);
        await expect(detail).toHaveAttribute("data-upload-url", /.+/);
        await expect(detail.locator("[data-album-back]")).toBeVisible();
    });
});
