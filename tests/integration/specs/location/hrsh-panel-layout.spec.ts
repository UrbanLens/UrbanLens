/**
 * Where the HRSH pin page puts its data: county- and watershed-scale sources as Regional Data tabs, the historic
 * register as a Location Data tab, and the National Register listing named in Location Data's Overview.
 */

import type { Page } from "@playwright/test";

import { expect, locationDataTest as test, openPrivatePin, skipUnlessLocationDataEnabled } from "./fixtures.js";
import { HRSH_NAME_PATTERN } from "../../lib/hrsh.js";
import { waitForOrNull } from "../../lib/waiting.js";

skipUnlessLocationDataEnabled();

/** Regional Data tabs, and what the loaded tab must say to be the right data. */
const REGIONAL_TABS = [
    { label: "Disasters", content: /federal disaster declaration/i },
    { label: "Water", content: /stream|wetland|watershed|waterbod/i },
    { label: "Air Quality", content: /AQI|PM2\.5|ozone/i },
] as const;

/** The DOM ids these sources had as cards of their own. Inside a tab body they are the tab's content, not a card. */
const FORMER_CARD_IDS = ["hazard-history-section", "hydrology-section", "air-quality-section", "historic-registers-section"];

const NRHP_MENTION = /National Register of Historic Places as “([^”]+)”/;

/** Long enough for a register fetch the Overview scheduled to land; the Overview itself polls for about a minute. */
const OVERVIEW_WAIT_MS = 240_000;
const OVERVIEW_DWELL_MS = 8_000;
const TAB_CONTENT_TIMEOUT_MS = 120_000;

function tabStrip(page: Page, sectionId: string) {
    return page.locator(`#${sectionId} > .card-tabs .pin-plugin-tab-btn`);
}

async function tabLabels(page: Page, sectionId: string): Promise<string[]> {
    return (await tabStrip(page, sectionId).locator("span").allTextContents()).map((label) => label.trim());
}

/** The Overview's text once it has settled, or null when it never showed the listing. */
async function overviewMentioningTheRegister(page: Page, slug: string): Promise<string | null> {
    return waitForOrNull(
        async () => {
            await openPrivatePin(page, slug, { metricPrefix: null });
            const body = page.locator("#location-data-body");
            await body.scrollIntoViewIfNeeded();
            await page.waitForTimeout(OVERVIEW_DWELL_MS);
            return (await body.innerText()).trim();
        },
        (text) => NRHP_MENTION.test(text),
        { what: "Location Data's Overview to name the National Register listing", timeoutMs: OVERVIEW_WAIT_MS, intervalMs: 20_000, describe: (text) => text.slice(0, 200) },
    );
}

test.describe("Hudson River State Hospital - panel layout on the private pin page", () => {
    test("county- and watershed-scale data are Regional Data tabs, not cards", async ({ campus, page }) => {
        await openPrivatePin(page, campus.pin.slug, { metricPrefix: null });

        const labels = await tabLabels(page, "pin-plugin-tabs-section");
        for (const { label } of REGIONAL_TABS) {
            expect(labels, `Regional Data has no ${label} tab`).toContain(label);
        }
        expect(await tabLabels(page, "location-data-section"), "Location Data has no Historic Registers tab").toContain("Historic Registers");

        const standalone = await page.evaluate(
            (ids) => ids.filter((id) => {
                const element = document.getElementById(id);
                return element !== null && element.closest(".pin-plugin-tab-body") === null;
            }),
            FORMER_CARD_IDS,
        );
        expect(standalone, "these sources still render as cards of their own as well as tabs").toEqual([]);
    });

    for (const { label, content } of REGIONAL_TABS) {
        test(`the Regional Data ${label} tab loads its own data`, async ({ campus, page }) => {
            await openPrivatePin(page, campus.pin.slug, { metricPrefix: null });
            const section = page.locator("#pin-plugin-tabs-section");
            await section.scrollIntoViewIfNeeded();

            const tab = tabStrip(page, "pin-plugin-tabs-section").filter({ hasText: label });
            await tab.click();
            await expect(tab).toHaveClass(/active/);

            const body = page.locator("#pin-plugin-tab-body");
            await expect(body, `the ${label} tab never showed its data`).toContainText(content, { timeout: TAB_CONTENT_TIMEOUT_MS });
            await expect(body.locator(".simple-info-panel.card"), "the tab's panel brought its own card into the strip's card").toHaveCount(0);
        });
    }

    test("Location Data's Overview names the National Register listing and opens its tab", async ({ campus, page }) => {
        const overview = await overviewMentioningTheRegister(page, campus.pin.slug);
        expect(
            overview,
            "the Overview never said the site is on the National Register. REData lists the Hudson River State Hospital Main Building (nps_nrhp) " +
                "for this point; check the Historic Registers tab first - if it is empty, the register fetch failed rather than the Overview",
        ).not.toBeNull();

        const named = NRHP_MENTION.exec(overview ?? "")?.[1] ?? "";
        expect(
            named,
            `the Overview named "${named}". Several listings are near this point (the Isaac Roosevelt House is nearer the pin than the hospital), and the ` +
                "one sharing words with the place's own name should win - a mismatch means the location's name is not the hospital's, or the ranking regressed",
        ).toMatch(HRSH_NAME_PATTERN);

        await page.locator('#location-data-body [data-open-tab="redata_historic_registers"]').click();
        const tab = tabStrip(page, "location-data-section").filter({ hasText: "Historic Registers" });
        await expect(tab, "the Overview's link did not switch to the Historic Registers tab").toHaveClass(/active/);
        await expect(page.locator("#location-data-body")).toContainText("National Register of Historic Places", { timeout: TAB_CONTENT_TIMEOUT_MS });
    });
});
