/**
 * No page may scroll sideways on a phone. A horizontal scrollbar at phone width is the one layout
 * fault that affects every page at once and that nobody notices on a desktop, which is how this one
 * survived: `.app-nav-right` ran 40px past a 390px viewport on every page in the application, and
 * it was first reported against the map because that is where somebody happened to look (P52).
 */

import { expect, test } from "../../lib/fixtures.js";
import { appRoutes } from "../../lib/routes.js";

/** Widths that real phones report, smallest first. */
const PHONE_WIDTHS = [320, 360, 390, 414] as const;

/** One page per top-level section; the nav is on all of them, so a handful is enough. */
const PAGES = [appRoutes.home, appRoutes.map, appRoutes.trips, appRoutes.organize, appRoutes.vaultHome];

/** Elements sticking out past `width`, shallowest first, ignoring clipped ones. */
async function offenders(page: import("@playwright/test").Page, width: number) {
    return page.evaluate((viewport) => {
        const isClipped = (element: Element): boolean => {
            for (let parent = element.parentElement; parent; parent = parent.parentElement) {
                const style = getComputedStyle(parent);
                if (style.overflowX !== "visible" || style.overflow !== "visible") {
                    return parent.getBoundingClientRect().right <= viewport + 0.5;
                }
            }
            return false;
        };
        const rows: { selector: string; right: number; width: number; depth: number }[] = [];
        for (const element of document.querySelectorAll("body *")) {
            const box = element.getBoundingClientRect();
            if (box.width === 0 || box.height === 0 || box.right <= viewport + 0.5) continue;
            if (isClipped(element)) continue;
            let depth = 0;
            for (let parent = element.parentElement; parent; parent = parent.parentElement) depth += 1;
            const classes = typeof element.className === "string" && element.className.trim() ? `.${element.className.trim().split(/\s+/).join(".")}` : "";
            rows.push({
                selector: `${element.tagName.toLowerCase()}${element.id ? `#${element.id}` : ""}${classes}`,
                right: Math.round(box.right),
                width: Math.round(box.width),
                depth,
            });
        }
        return rows.sort((a, b) => a.depth - b.depth).slice(0, 5);
    }, width);
}

test.describe("responsive layout", () => {
    for (const width of PHONE_WIDTHS) {
        test(`no page scrolls sideways at ${width}px`, async ({ page }) => {
            await page.setViewportSize({ width, height: 780 });

            for (const path of PAGES) {
                await page.goto(path, { waitUntil: "domcontentloaded" });
                // Not networkidle: the app holds a Channels socket open, so the
                // network never goes idle and the wait times out instead.
                await page.waitForSelector(".app-nav-inner", { state: "visible" });

                const overflow = await page.evaluate(() => ({
                    scrollWidth: document.documentElement.scrollWidth,
                    clientWidth: document.documentElement.clientWidth,
                }));

                expect(
                    overflow.scrollWidth,
                    `${path} scrolls sideways at ${width}px: content is ${overflow.scrollWidth}px wide in a ${overflow.clientWidth}px viewport. Widest unclipped offenders: ${JSON.stringify(await offenders(page, width))}`,
                ).toBeLessThanOrEqual(overflow.clientWidth);
            }
        });
    }
});
