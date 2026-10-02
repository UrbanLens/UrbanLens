/**
 * A toast raised while a modal dialog is open is drawn above it (P194).
 *
 * A modal dialog sits in the top layer with a backdrop over the rest of the page, so the toast stack, which was not,
 * came out dimmed behind the backdrop - the error a dialog's own submit raised was the one hardest to read. The stack
 * is now lifted into the top layer above the dialog. It is still outside the dialog, so the modal leaves it inert:
 * hit-testing passes through it, which is why this compares what is drawn rather than what a point lands on.
 */

import type { Page } from "@playwright/test";

import { expect, test } from "../../lib/fixtures.js";
import { appRoutes } from "../../lib/routes.js";

declare global {
    interface Window {
        toastr?: Record<"success" | "error" | "warning" | "info", (message: string) => void> & { clear(): void };
    }
}

const TOAST = "#toast-container > *";

/** The newest toast's box, once it has finished sliding in from the right edge. */
async function settledToast(page: Page) {
    await expect
        .poll(() =>
            page.evaluate((selector) => {
                const box = document.querySelector(selector)?.getBoundingClientRect();
                // The newest is on top of the stack, so only its right edge says where the corner is.
                return box ? window.innerWidth - box.right > 0 && window.innerWidth - box.right < 48 && box.bottom < window.innerHeight : false;
            }, TOAST),
        )
        .toBe(true);
    const box = await page.locator(TOAST).first().boundingBox();
    if (!box) throw new Error("the toast has no box");
    return box;
}

/** The brightest channel average in the toast's pixels: its text in the dark theme, its fill in the light one. */
async function peakBrightness(page: Page): Promise<number> {
    const png = await page.screenshot({ clip: await settledToast(page) });
    // Decoded in a blank page: the app's CSP would refuse the data: image.
    const blank = await page.context().newPage();
    try {
        return await blank.evaluate(async (data) => {
            const image = new Image();
            image.src = `data:image/png;base64,${data}`;
            await image.decode();
            const canvas = document.createElement("canvas");
            canvas.width = image.width;
            canvas.height = image.height;
            const context = canvas.getContext("2d");
            if (!context) throw new Error("no 2d context");
            context.drawImage(image, 0, 0);
            const pixels = context.getImageData(0, 0, image.width, image.height).data;
            let peak = 0;
            for (let i = 0; i < pixels.length; i += 4) peak = Math.max(peak, ((pixels[i] ?? 0) + (pixels[i + 1] ?? 0) + (pixels[i + 2] ?? 0)) / 3);
            return peak;
        }, png.toString("base64"));
    } finally {
        await blank.close();
    }
}

async function toastWithoutDialog(page: Page): Promise<number> {
    await page.evaluate(() => window.toastr?.info("A toast"));
    const peak = await peakBrightness(page);
    await page.evaluate(() => window.toastr?.clear());
    await expect(page.locator(TOAST)).toHaveCount(0);
    return peak;
}

test.beforeEach(async ({ page }) => {
    await page.goto(appRoutes.settings);
    await page.evaluate(() => {
        const dialog = document.createElement("dialog");
        dialog.id = "p194-dialog";
        dialog.className = "ul-dialog";
        dialog.textContent = "A dialog covering the page";
        document.body.append(dialog);
    });
});

test("a toast raised over an open modal dialog is as bright as one with no dialog", async ({ page }) => {
    const alone = await toastWithoutDialog(page);
    await page.evaluate(() => {
        document.querySelector<HTMLDialogElement>("#p194-dialog")?.showModal();
        window.toastr?.info("A toast");
    });

    expect(await peakBrightness(page)).toBeGreaterThan(alone * 0.9);
});

test("the comparison sees a toast left under the backdrop", async ({ page }) => {
    const alone = await toastWithoutDialog(page);
    await page.evaluate(() => {
        // What the page did before P194: nothing lifts the stack.
        Object.defineProperty(HTMLElement.prototype, "showPopover", { value: undefined });
        document.querySelector<HTMLDialogElement>("#p194-dialog")?.showModal();
        window.toastr?.info("A toast");
    });

    expect(await peakBrightness(page)).toBeLessThan(alone * 0.8);
});

test("a toast raised before a dialog opens is lifted above it with the next toast", async ({ page }) => {
    const alone = await toastWithoutDialog(page);
    await page.evaluate(() => {
        window.toastr?.info("Before the dialog");
        document.querySelector<HTMLDialogElement>("#p194-dialog")?.showModal();
        window.toastr?.info("A toast");
    });

    expect(await peakBrightness(page)).toBeGreaterThan(alone * 0.9);
});

test("with no dialog open a toast still sits in the corner and takes a click", async ({ page }) => {
    await page.evaluate(() => window.toastr?.success("No dialog"));
    const box = await settledToast(page);

    const hit = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.closest("#toast-container") !== null, { x: box.x + box.width / 2, y: box.y + box.height / 2 });
    expect(hit).toBe(true);
});
