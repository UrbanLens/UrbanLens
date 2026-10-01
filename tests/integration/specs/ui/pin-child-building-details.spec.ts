/**
 * A building child's own details open in place from its property's "Buildings on this Property" list (P172).
 * Only that list's own requests are let through, so no panel is fetched and no upstream is spent by the page.
 */

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import { PinDetailPage } from "../../lib/pages/pin-detail-page.js";
import { pinDetail } from "../../lib/routes.js";

/** About 5 m north: inside the property, and within the app's 15 m "same building" radius of the parent. */
const CHILD_OFFSET_DEG = 0.000045;

test.describe("pin detail - child pin details", () => {
    test("a building child's details open in place from its property's list", async ({ page, api, guard }) => {
        // htmx logs each request this test aborts.
        guard.allow(/htmx:(sendAbort|sendError|afterRequest)/);
        const parent = await api.createPin();
        const name = resourceName("carriage house");
        const description = `Slate roof, rear wall down (${resourceName("note")}).`;
        const child = await api.json<{ uuid: string; slug: string }>("post", "pins/", {
            name,
            name_is_user_provided: true,
            description,
            latitude: parent.latitude + CHILD_OFFSET_DEG,
            longitude: parent.longitude,
            pin_type: "building",
            parent_id: parent.uuid,
        });
        api.track("pin", child.slug, () => api.delete(`pins/${child.slug}/`));

        await page.route("**/*", (route) => {
            const request = route.request();
            if (!["xhr", "fetch"].includes(request.resourceType())) return route.continue();
            return /\/(buildings|building-card)\/(\?|$)/.test(request.url()) ? route.continue() : route.abort("aborted");
        });

        const detail = new PinDetailPage(page);
        await page.goto(pinDetail(parent.slug));
        await detail.expectLoaded();

        // Pending until the property's building data has been fetched, which a fresh location has to wait for.
        const section = page.locator("#parcel-buildings-section");
        await expect(section, "the property's list of buildings and child pins never loaded").toBeVisible({ timeout: 120_000 });
        const childrenTab = section.locator(".card-tab", { hasText: "Child pins" });
        if ((await childrenTab.count()) > 0) await childrenTab.click();

        const row = section.locator('[data-pb-panel="children"] .child-pin-row', { hasText: name });
        await expect(row, `"${name}" is not among the property's child pins`).toHaveCount(1);
        await row.locator("summary .parcel-building-chevron").click();

        const card = row.locator(".child-building-detail");
        await expect(card, `"${name}" did not open in place`).toBeVisible();
        await expect(card).toContainText(description);
        await expect(card.getByRole("link", { name: /open/i })).toHaveAttribute("href", new RegExp(`${child.slug}/?$`));
    });
});
