/**
 * No page may scroll sideways on phone or tablet. Horizontal scrollbar at
 * those widths affects every page and goes unnoticed on desktop. The nav bar
 * ran 40px past 390px viewport; at 768px the full link row needed 837px.
 */

import { expect, test } from "../../lib/fixtures.js";
import { appRoutes } from "../../lib/routes.js";

/**
 * Phone widths that real devices report, then both ends of the band where the full link row shows
 * beside the right-hand group: 768 is where the hamburger goes away, 1000 a narrowed laptop window.
 */
const WIDTHS = [320, 360, 390, 414, 768, 1000] as const;

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
    for (const width of WIDTHS) {
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
