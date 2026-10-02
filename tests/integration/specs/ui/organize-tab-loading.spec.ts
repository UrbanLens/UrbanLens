/**
 * The Organize page loads each deferred panel once, and a hidden section not at all until it is shown (P191).
 *
 * htmx counts a hidden element as revealed, so `hx-trigger="revealed"` loaded every hidden panel at once and the
 * prewarm chain loaded it again.
 */

import { expect, test } from "../../lib/fixtures.js";
import { appRoutes } from "../../lib/routes.js";

const DEFERRED = /\/dashboard\/(?:category|status|people|media)\/rows\/$|\/dashboard\/organize\/priority\/list\/$|\/dashboard\/lists\/\?tab=(?:lists|filters)$/;

test("each deferred panel loads once, and a hidden section waits until it is opened", async ({ page }) => {
    const requested: string[] = [];
    page.on("request", (request) => {
        const url = new URL(request.url());
        if (DEFERRED.test(url.pathname + url.search)) requested.push(url.pathname + url.search);
    });

    await page.goto(`${appRoutes.organize}?tab=tags`);
    await page.locator(".organize-tab[data-tab='media']").click();
    await expect(page.locator("#panel-media .organize-section-loading")).toHaveCount(0, { timeout: 15_000 });
    await expect.poll(() => requested.filter((url) => url.includes("/rows/")).length, { timeout: 15_000 }).toBe(4);
    await expect.poll(() => requested.filter((url) => url.includes("/priority/")).length).toBe(1);

    expect(requested.filter((url) => url.includes("tab="))).toEqual([]);
    await page.locator(".organize-section-tab[data-section='lists']").click();
    await expect.poll(() => requested.filter((url) => url.endsWith("tab=lists")).length).toBe(1);

    const counts = new Map<string, number>();
    for (const url of requested) counts.set(url, (counts.get(url) ?? 0) + 1);
    expect([...counts].filter(([, n]) => n > 1)).toEqual([]);
});
