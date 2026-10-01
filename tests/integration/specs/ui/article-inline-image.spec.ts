/**
 * An image uploaded into a pin's article through the editor.
 *
 * The upload's raw file is deleted when its re-encode lands, so the link the editor writes into the article
 * must survive that: it names the Image row, which answers with a placeholder until the photo is ready.
 */

import type { Page } from "@playwright/test";

import { expect, test } from "../../lib/fixtures.js";
import { PinDetailPage } from "../../lib/pages/pin-detail-page.js";

const TINY_JPEG_BASE = Buffer.from(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBD/wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCdABmX/9k=",
    "base64",
);

/** The placeholder SVG is 640 wide; the photo is 1px, so the natural width says which one an <img> holds. */
const PHOTO_WIDTH = 1;
const PLACEHOLDER_WIDTH = 640;

function watchMissingMedia(page: Page): string[] {
    const missing: string[] = [];
    page.on("response", (response) => {
        if (response.status() === 404 && /\/media\//.test(response.url())) missing.push(response.url());
    });
    return missing;
}

function naturalWidthOf(page: Page, link: string): Promise<number> {
    return page
        .locator(`#article-panel img[data-link="${link}"]`)
        .first()
        .evaluate((img: HTMLImageElement) => (img.complete ? img.naturalWidth : -1));
}

test.describe("article inline image", () => {
    test("the link written into the article still shows the photo after its re-encode", async ({ page, api }) => {
        const missing = watchMissingMedia(page);
        const pin = await api.createPin();
        const detail = new PinDetailPage(page);
        await detail.goto(pin.slug);
        await detail.openTab("article");

        const editor = page.locator("#article-panel .ProseMirror");
        await expect(editor).toBeVisible();
        await editor.click();
        await editor.pressSequentially("Boiler room ");

        const chooser = page.waitForEvent("filechooser");
        const uploaded = page.waitForResponse((response) => response.request().method() === "POST" && response.url().includes("/article/image/"));
        // The toolbar is only shown in Source mode; in WYSIWYG mode its buttons still drive the editor.
        await page.locator('#article-panel [data-md-action="image"]').evaluate((button: HTMLElement) => button.click());
        await (await chooser).setFiles({ name: "inline.jpg", mimeType: "image/jpeg", buffer: Buffer.concat([TINY_JPEG_BASE, Buffer.from(`${Date.now()}`)]) });
        const response = await uploaded;
        expect(response.status(), await response.text()).toBe(201);
        const { url: link } = (await response.json()) as { url: string };
        expect(link, "the editor must be given a link to the row, not the raw file").toMatch(/^\/media\/image\/[0-9a-f-]{36}\/$/);

        // Placeholder while processing, then the photo, in the same editor without a reload.
        const seen = new Set<number>();
        await expect
            .poll(
                async () => {
                    const width = await naturalWidthOf(page, link);
                    seen.add(width);
                    return width;
                },
                { timeout: 90000, intervals: [250] },
            )
            .toBe(PHOTO_WIDTH);
        expect([...seen].filter((width) => ![-1, 0, PLACEHOLDER_WIDTH, PHOTO_WIDTH].includes(width)), `widths seen: ${[...seen].join(", ")}`).toEqual([]);
        test.info().annotations.push({ type: "editor states", description: [...seen].join(", ") });

        const saved = page.waitForResponse((r) => r.request().method() === "POST" && r.url().includes("/article/save/"));
        await page.locator("#article-panel .article-editor-actions button[type=submit]").click();
        expect((await saved).status()).toBe(200);

        await detail.goto(pin.slug);
        await detail.openTab("article");
        await expect(page.locator(`#article-panel img[data-link="${link}"]`)).toBeAttached();
        await expect.poll(() => naturalWidthOf(page, link), { timeout: 15000 }).toBe(PHOTO_WIDTH);
        expect(missing, "no media request may 404").toEqual([]);
    });
});
