/**
 * The News panel on the campus pin. Staging once filled it with Chinese rural-revitalisation stories,
 * because the query was the service road's name ("Courtyard Drive") and GDELT matches machine
 * translations. Whatever the panel shows must be about this place, in English. Showing nothing passes.
 */

import { expect, locationDataTest as test, skipUnlessLocationDataEnabled } from "./fixtures.js";
import { waitForOrNull } from "../../lib/waiting.js";

skipUnlessLocationDataEnabled();

/** A headline about this place names it or its town. */
const ABOUT_HRSH = /hudson\s+river\s+state\s+hospital|\bhrsh\b|poughkeepsie/i;

/** Largest share of a headline's letters allowed outside Latin script. */
const MAX_NON_LATIN_SHARE = 0.2;

const NEWS_READY_TIMEOUT_MS = 180_000;

interface PanelEntry {
    key: string;
    ready: boolean;
}

interface NewsCard {
    info?: { meta?: Array<{ value?: string; href?: string }> };
}

function nonLatinShare(text: string): number {
    const letters = [...text].filter((character) => /\p{L}/u.test(character));
    if (letters.length === 0) {
        return 0;
    }
    return letters.filter((character) => !/\p{Script=Latin}/u.test(character)).length / letters.length;
}

function offTopic(headlines: string[]): string[] {
    return headlines.filter((headline) => !ABOUT_HRSH.test(headline) || nonLatinShare(headline) > MAX_NON_LATIN_SHARE);
}

test.describe("Hudson River State Hospital - news", () => {
    test("every news item shown names the place and is in English", async ({ campus, page }) => {
        const panelsPath = `pins/${campus.pin.slug}/panels/`;
        // A 5xx while polling counts as "not yet": the dev stack restarts under other work.
        const readPanels = async (): Promise<PanelEntry[]> => {
            const response = await campus.api.get(panelsPath);
            if (!response.ok()) {
                return [];
            }
            const body = (await response.json()) as PanelEntry[] | { results?: PanelEntry[] };
            return Array.isArray(body) ? body : (body.results ?? []);
        };
        const ready = await waitForOrNull(readPanels, (listed) => listed.some((panel) => panel.key === "gdelt" && panel.ready), {
            what: "the News panel to become ready",
            timeoutMs: NEWS_READY_TIMEOUT_MS,
            intervalMs: 10_000,
        });
        test.skip(
            ready === null,
            "the News panel was not offered (no REData) or never became ready (GDELT throttles often); an empty panel is not what this checks.",
        );

        const response = await campus.api.get(`${panelsPath}gdelt/`);
        const apiHeadlines = response.status() === 204 ? [] : ((await response.json()) as NewsCard).info?.meta?.map((row) => row.value ?? "") ?? [];
        expect(offTopic(apiHeadlines), `news items on the API that are not about HRSH, or not in English: ${JSON.stringify(apiHeadlines)}`).toEqual([]);

        const fragment = await page.request.get(`/dashboard/map/pin/${campus.pin.slug}/panel/gdelt/`);
        const html = fragment.status() === 204 ? "" : await fragment.text();
        await page.setContent(html);
        const pageHeadlines = await page.locator(".simple-info-meta a").allInnerTexts();
        expect(offTopic(pageHeadlines), `news items on the pin page that are not about HRSH, or not in English: ${JSON.stringify(pageHeadlines)}`).toEqual([]);
    });
});
