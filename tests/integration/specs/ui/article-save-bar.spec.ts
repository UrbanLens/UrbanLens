/**
 * The article editor's unsaved-changes bar, the pin page's floating actions and the sticky page footer are all fixed
 * to the bottom of the viewport. At 1280px the actions sat over Save; on phones the footer, which wraps to three
 * lines there, sat over it too.
 */

import type { Page } from "@playwright/test";

import { expect, test } from "../../lib/fixtures.js";
import { PinDetailPage } from "../../lib/pages/pin-detail-page.js";

/** Whether a click at the centre of `selector` lands on it rather than on something drawn over it. */
function receivesClicks(page: Page, selector: string): Promise<boolean> {
    return page.locator(selector).first().evaluate((element) => {
        const box = element.getBoundingClientRect();
        const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
        return hit !== null && (hit === element || element.contains(hit));
    });
}

for (const viewport of [
    { width: 1280, height: 720 },
    { width: 412, height: 915 },
    { width: 320, height: 640 },
]) {
    test.describe(`article save bar at ${viewport.width}px`, () => {
        test.use({ viewport });

        test("Save and the floating actions are both reachable while the article has unsaved changes", async ({ page, api }) => {
            const pin = await api.createPin();
            const detail = new PinDetailPage(page);
            await detail.goto(pin.slug);
            await detail.openTab("article");

            const editor = page.locator("#article-panel .ProseMirror");
            await editor.click();
            await editor.pressSequentially("Boiler room");
            const save = "#article-panel .article-editor-actions button[type=submit]";
            await expect(page.locator(save)).toBeVisible();
            await expect(page.locator("#pin-actions-fab .pin-actions-fab-btn")).toBeVisible();

            expect(await receivesClicks(page, save), "something is drawn over the article's Save button").toBe(true);
            expect(await receivesClicks(page, "#pin-actions-fab .pin-actions-fab-btn"), "something is drawn over the Actions button").toBe(true);
        });
    });
}
