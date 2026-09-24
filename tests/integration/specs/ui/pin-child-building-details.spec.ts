/**
 * The "child pin details" toggle on a property's page brings its building child's own details up to the top level.
 * Only the building section's own requests are let through, so no panel is fetched and no upstream is spent.
 */

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import { PinDetailPage } from "../../lib/pages/pin-detail-page.js";
import { pinDetail } from "../../lib/routes.js";

/** About 5 m north: inside the property, and within the app's 15 m "same building" radius of the parent. */
const CHILD_OFFSET_DEG = 0.000045;

test.describe("pin detail - child pin details", () => {
    test("a building child's details show on its property's page only while the toggle is on", async ({ page, api, guard }) => {
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
            return /\/(child-buildings|building-card)\//.test(request.url()) ? route.continue() : route.abort("aborted");
        });

        const detail = new PinDetailPage(page);
        await page.goto(`${pinDetail(parent.slug)}?children=1`);
        await detail.expectLoaded();

        const section = page.locator('[data-tab-panel="overview"] .pin-overview-stack > #child-buildings-section');
        await expect(section, "the building section never loaded at the top of the Overview").toBeVisible();
        const heading = `Building: ${name}`;
        const card = section.locator(".child-building-card", { hasText: heading });
        if ((await card.count()) === 0) {
            // A sweep may have given the property other buildings, one of which holds the pin.
            await section.locator(".child-building-row summary", { hasText: heading }).click();
        }
        await expect(card, `no "${heading}" card on the property's page`).toBeVisible();
        await expect(card).toContainText(description);
        await expect(card.getByRole("link", { name: /open/i })).toHaveAttribute("href", new RegExp(`${child.slug}/?$`));

        await page.locator("#pin-actions-fab-btn").click();
        const toggle = page.locator(".pin-actions-item", { hasText: "Child pin details" });
        await expect(toggle.locator(".pin-actions-state")).toHaveText("On");
        await toggle.click();
        await page.waitForURL(/[?&]children=0/);
        await detail.expectLoaded();

        await expect(page.locator("#child-buildings-section"), "the building section is still on the page with the toggle off").toHaveCount(0);
        await expect(page.getByText(heading), "the building's details are still on its property's page with the toggle off").toHaveCount(0);
        await expect(page.getByText(description)).toHaveCount(0);
    });
});
