/**
 * Reading the toast notifications the app reports every result through.
 *
 * `CLAUDE.md` states the rule these helpers encode: results and errors surface
 * as toasts. That makes a toast the single most reliable place to assert what
 * the server actually decided - more reliable than re-reading the page, because
 * a failed action frequently leaves the page looking exactly as it did before.
 *
 * Toasts are rendered by toastr 2.1.4 (loaded in `themes/base.html`) with a
 * 4.5-second timeout, so an assertion has to be made promptly after the action
 * that provoked it rather than at the end of a long test.
 */

import { expect, type Locator, type Page } from "@playwright/test";

export type ToastLevel = "success" | "info" | "warning" | "error";

/** Every toast currently on screen, newest first (`newestOnTop` is on). */
export function toasts(page: Page, level?: ToastLevel): Locator {
    const selector = level ? `#toast-container .toast-${level}` : "#toast-container .toast";
    return page.locator(selector);
}

/**
 * Asserts a toast of `level` appears, optionally matching `text`.
 *
 * @returns The matched toast, so a caller can read more out of it.
 */
export async function expectToast(page: Page, level: ToastLevel, text?: string | RegExp): Promise<Locator> {
    const locator = text ? toasts(page, level).filter({ hasText: text }) : toasts(page, level);
    await expect(locator.first()).toBeVisible();
    return locator.first();
}

/**
 * Asserts no error toast appeared.
 *
 * Point in time, not a promise about the future: call it after the action whose
 * silence is being asserted, not before.
 */
export async function expectNoErrorToast(page: Page): Promise<void> {
    const errors = toasts(page, "error");
    const count = await errors.count();
    if (count > 0) {
        const messages = await errors.allInnerTexts();
        throw new Error(`Expected no error toast, but ${count} appeared:\n  ${messages.join("\n  ")}`);
    }
}

/**
 * Clears any visible toast.
 *
 * Worth doing between two actions in one test: toastr stacks, and a stale
 * success from step one will satisfy an assertion meant for step three.
 */
export async function dismissToasts(page: Page): Promise<void> {
    await page.evaluate(() => {
        const container = document.getElementById("toast-container");
        container?.replaceChildren();
    });
    await expect(toasts(page)).toHaveCount(0);
}
