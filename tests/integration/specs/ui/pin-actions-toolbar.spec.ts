/**
 * The floating actions toolbar on the pin page (`partials/ui/_hierarchy_actions_fab.html`, `shared/actions-fab.ts`).
 * Each check reads what the browser renders - a `hidden` attribute that a stylesheet overrides still paints.
 */

import { type Locator, type Page } from "@playwright/test";

import { expect, test } from "../../lib/fixtures.js";
import { PinDetailPage } from "../../lib/pages/pin-detail-page.js";

const fab = (page: Page): Locator => page.locator("#pin-actions-fab");
const actionsButton = (page: Page): Locator => page.locator("#pin-actions-fab-btn");
const collapseButton = (page: Page): Locator => page.locator("#pin-actions-collapse");
const undoButton = (page: Page): Locator => page.locator("#pin-actions-undo");

/** True when the element paints with real width and is not faded out by an ancestor. */
async function painted(locator: Locator): Promise<boolean> {
    return locator.evaluate((el) => {
        const rect = el.getBoundingClientRect();
        if (rect.width < 4 || rect.height < 4) return false;
        for (let node: Element | null = el; node; node = node.parentElement) {
            const style = getComputedStyle(node);
            if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity) < 0.1) return false;
        }
        return true;
    });
}

async function openPin(page: Page, api: { createPin: () => Promise<{ slug: string }> }): Promise<PinDetailPage> {
    const pin = await api.createPin();
    const detail = new PinDetailPage(page);
    await detail.goto(pin.slug);
    await expect(fab(page)).toBeAttached();
    if (await painted(actionsButton(page))) await actionsButton(page).click();
    return detail;
}

test.describe("pin actions toolbar", () => {
    test("the Actions button shows only while the toolbar is closed", async ({ page, api }) => {
        await openPin(page, api);

        expect(await painted(actionsButton(page)), "the Actions button paints while the toolbar is open").toBe(false);
        expect(await painted(undoButton(page))).toBe(true);

        await collapseButton(page).click();
        await expect.poll(() => painted(actionsButton(page))).toBe(true);
        expect(await painted(undoButton(page))).toBe(false);
    });

    test("the toolbar's tools are there every time it reopens", async ({ page, api }) => {
        await openPin(page, api);

        for (let cycle = 1; cycle <= 3; cycle++) {
            await collapseButton(page).click();
            await actionsButton(page).click();
            await expect.poll(() => painted(undoButton(page)), { message: `reopen ${cycle} shows no tools` }).toBe(true);
        }
    });

    test("article-only tools show only on the Article tab", async ({ page, api }) => {
        const detail = await openPin(page, api);
        const articleOnly = page.locator("#pin-actions-menu [data-article-page-action]");
        await expect(articleOnly).toHaveCount(2);

        for (const tool of await articleOnly.all()) expect(await painted(tool), "an article tool paints on the Overview tab").toBe(false);
        await detail.openTab("article");
        for (const tool of await articleOnly.all()) await expect.poll(() => painted(tool)).toBe(true);
        await detail.openTab("photos");
        for (const tool of await articleOnly.all()) expect(await painted(tool), "an article tool paints on the Photos tab").toBe(false);
    });

    test("the open toolbar is nearly opaque, and fully so on hover", async ({ page, api }) => {
        await openPin(page, api);
        await page.mouse.move(5, 5);
        await expect.poll(() => fab(page).evaluate((el) => Number(getComputedStyle(el).opacity))).toBeCloseTo(0.9, 2);
        await fab(page).hover();
        await expect.poll(() => fab(page).evaluate((el) => Number(getComputedStyle(el).opacity))).toBe(1);
    });

    test("Hidden sections is offered while a section is hidden, and opens a menu of them", async ({ page, api }) => {
        await openPin(page, api);
        const hiddenSections = page.locator("#pin-actions-hidden-sections");
        const alreadyHidden = await page.locator("[data-collapse-section].is-collapsed").evaluateAll((els) => els.filter((el) => !el.closest("[hidden]")).length);
        expect(await painted(hiddenSections), "Hidden sections disagrees with what is hidden").toBe(alreadyHidden > 0);

        const section = page.locator("[data-collapse-section]:not(.is-collapsed)").filter({ has: page.locator(".section-collapse-btn:visible") }).first();
        const label = (await section.getAttribute("data-collapse-label")) || ((await section.getAttribute("data-collapse-section")) ?? "");
        await section.locator(".section-collapse-btn").first().click();
        try {
            await expect.poll(() => painted(hiddenSections)).toBe(true);
            await hiddenSections.click();
            const menu = page.locator("#tools-fab-menu");
            await expect(menu).toBeVisible();
            await expect(menu.locator(".collapse-restore-item", { hasText: label })).toHaveCount(1);
        } finally {
            await page.evaluate(() => localStorage.clear());
        }
    });
});
