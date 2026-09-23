/**
 * The private pin detail page itself, right after pinning HRSH: does the map draw the real parcel,
 * does the gallery fill itself in, does the seeded article actually render. Companion files check the
 * same data through the API; this one checks what a user looking at the page would see.
 */

import type { Page } from "@playwright/test";

import { expect, locationDataTest as test, openPrivatePin, skipUnlessLocationDataEnabled } from "./fixtures.js";
import { approximateAreaSqm, containsCoordinate, EXPECTED_PARCEL_AREA_SQM, HRSH_PIN, hrshRoutes } from "../../lib/hrsh.js";
import { waitForHtmxSettled } from "../../lib/htmx.js";
import { recordMetric, timed } from "../../lib/metrics.js";
import { waitForOrNull } from "../../lib/waiting.js";

skipUnlessLocationDataEnabled();

/** Map tile origins that fail in this environment for reasons unrelated to this page. */
const THIRD_PARTY_TILE_HOSTS = [/wayback\.maptiles\.arcgis\.com/, /tile\.openstreetmap\.org/, /server\.arcgisonline\.com/];

/** Boundary sources meaning a provider (or a person) actually supplied the outline. */
const HONEST_BOUNDARY_SOURCES = new Set(["place", "pin", "wiki", "inherited"]);

/** The property boundary's stroke colour (BOUNDARY_STYLES.property in map-annotations.ts); the fallback circle shares it, styled with a dashed stroke instead. */
const PROPERTY_STROKE_COLOR = "#cc2200";

/** Every registered media-loader source except the user's own uploads ("photos"). */
const SEARCH_RESULT_SOURCES = new Set([
    "smithsonian",
    "wikimedia",
    "wikipedia_media",
    "loc",
    "internet_archive",
    "digital_commonwealth",
    "yelp",
    "google_images",
    "searxng_images",
    "google_maps",
    "loopnet",
    "cris_building",
]);

const GALLERY_SETTLE_MS = 150_000;
const GALLERY_POLL_MS = 2_500;
const GALLERY_STABLE_POLLS = 4;

interface PinBoundaryPayload {
    boundaries?: { property?: { source?: string | null } };
}

function pinPath(slug: string): string {
    return `/dashboard/map/pin/${slug}/`;
}

/** `data-media-source` of every tile currently in the gallery. */
async function mediaSources(page: Page): Promise<string[]> {
    return page.locator(".media-item").evaluateAll((items) => items.map((item) => item.getAttribute("data-media-source") ?? ""));
}

/** Polls until the tile count stops changing (mirrors hrsh-media.spec.ts's settleGallery). */
async function settleMediaSources(page: Page): Promise<string[]> {
    const deadline = Date.now() + GALLERY_SETTLE_MS;
    let previous = -1;
    let unchanged = 0;
    let sources: string[] = [];
    while (Date.now() < deadline) {
        sources = await mediaSources(page);
        if (sources.length === previous) {
            unchanged += 1;
        } else {
            unchanged = 0;
            previous = sources.length;
        }
        if ((await page.locator(".media-provider-loader").count()) === 0 || unchanged >= GALLERY_STABLE_POLLS) {
            return sources;
        }
        await page.waitForTimeout(GALLERY_POLL_MS);
    }
    return sources;
}

test.describe("Hudson River State Hospital - the private pin detail page", () => {
    test.beforeEach(async ({ guard }) => {
        for (const host of THIRD_PARTY_TILE_HOSTS) {
            guard.allow(host);
        }
    });

    test("opening the page requests its own boundary payload and draws the resolved parcel", async ({ campus, page }) => {
        campus.requireBoundary();

        const boundaryPath = hrshRoutes.pinBoundary(campus.pin.slug);
        const boundaryRequest = page.waitForResponse((response) => new URL(response.url()).pathname === boundaryPath, { timeout: 30_000 });
        const startedAt = Date.now();
        await page.goto(pinPath(campus.pin.slug));

        const boundaryResponse = await boundaryRequest;
        expect(boundaryResponse.status(), "the page never requested its own /boundary/ endpoint - map-annotations.ts should fetch it unconditionally on load").toBe(200);

        const drawn = page.locator("#pin-detail-map-wrapper .leaflet-pane path");
        await expect(drawn.first(), "no vector was drawn on the pin detail map after its own boundary request answered").toBeAttached({ timeout: 60_000 });
        recordMetric({ name: "hrsh.pin_page.seconds_to_boundary_drawn", value: Math.round((Date.now() - startedAt) / 1000), unit: "s" });
    });

    test("the drawn parcel is an honest provider polygon, not the fallback circle's dashed style", async ({ campus, page }) => {
        campus.requireBoundary();

        const response = await page.request.get(hrshRoutes.pinBoundary(campus.pin.slug));
        expect(response.status(), "the boundary endpoint did not answer").toBe(200);
        const source = ((await response.json()) as PinBoundaryPayload).boundaries?.property?.source ?? null;
        expect(
            HONEST_BOUNDARY_SOURCES.has(source ?? ""),
            `the property boundary's source is ${JSON.stringify(source)}, which is neither a provider's outline nor a person's drawing`,
        ).toBe(true);

        await page.goto(pinPath(campus.pin.slug));
        const propertyPath = page.locator(`#pin-detail-map-wrapper .leaflet-pane path[stroke="${PROPERTY_STROKE_COLOR}"]`).first();
        await expect(propertyPath, "no property-styled boundary path was drawn on the map").toBeAttached({ timeout: 60_000 });

        const dashArray = await propertyPath.getAttribute("stroke-dasharray");
        expect(
            dashArray,
            `the drawn property boundary has stroke-dasharray="${dashArray}", the 50 m fallback circle's dashed style (CIRCLE_STYLE in map-annotations.ts), ` +
                `even though its reported source is ${JSON.stringify(source)}`,
        ).not.toBe("6 6");
    });

    test("the parcel geometry is plausibly sized and contains the requirement's own coordinate", async ({ campus }) => {
        campus.requireBoundary();

        const area = approximateAreaSqm(campus.boundary);
        expect(
            area >= EXPECTED_PARCEL_AREA_SQM.min && area <= EXPECTED_PARCEL_AREA_SQM.max,
            `the parcel measures ${Math.round(area).toLocaleString()} m², outside the plausible ${EXPECTED_PARCEL_AREA_SQM.min.toLocaleString()}-` +
                `${EXPECTED_PARCEL_AREA_SQM.max.toLocaleString()} m² range for this site`,
        ).toBe(true);
        expect(
            containsCoordinate(campus.boundary, HRSH_PIN),
            `the drawn parcel does not contain the requirement's own pinned coordinate (${HRSH_PIN.latitude}, ${HRSH_PIN.longitude})`,
        ).toBe(true);
    });

    test("a search-result photo source populates the gallery automatically, with attribution", async ({ campus, page }) => {
        const startedAt = Date.now();
        await page.goto(pinPath(campus.pin.slug));

        const settled = await waitForOrNull(() => mediaSources(page), (sources) => sources.some((source) => SEARCH_RESULT_SOURCES.has(source)), {
            what: "a media tile from a search-result provider (searxng_images, google_images, wikimedia, loc, ...)",
            timeoutMs: 180_000,
            intervalMs: 5_000,
            describe: (sources) => (sources.length ? `sources seen: ${[...new Set(sources)].join(", ")}` : "no media tiles at all"),
        });
        expect(
            settled,
            "no search-result photo arrived without the user clicking anything. Check the media-provider loaders' hx-trigger and that " +
                "the account's external_apis_enabled is on",
        ).not.toBeNull();
        recordMetric({ name: "hrsh.pin_page.seconds_to_first_search_photo", value: Math.round((Date.now() - startedAt) / 1000), unit: "s" });

        const foundSource = settled!.find((source) => SEARCH_RESULT_SOURCES.has(source))!;
        const attribution = ((await page.locator(`.media-item[data-media-source="${foundSource}"] .media-item-source`).first().textContent()) ?? "").trim();
        expect(attribution, `the first ${foundSource} tile carries no provider attribution text`).not.toBe("");
    });

    test("every media source that finds something is counted, and at least one is a search-result source", async ({ campus, page }) => {
        await page.goto(pinPath(campus.pin.slug));
        const sources = await settleMediaSources(page);
        test.skip(sources.length === 0, "no media arrived at all - the previous test reports that as the finding.");

        const counts = new Map<string, number>();
        for (const source of sources) {
            counts.set(source, (counts.get(source) ?? 0) + 1);
        }
        for (const [source, count] of counts) {
            recordMetric({ name: "hrsh.pin_page.media_tile_count", value: count, unit: "count", tags: { source } });
        }
        const searchSources = [...counts.keys()].filter((source) => SEARCH_RESULT_SOURCES.has(source));
        recordMetric({ name: "hrsh.pin_page.search_source_count", value: searchSources.length, unit: "count" });

        expect(
            searchSources.length,
            `media tiles arrived from: ${[...counts.keys()].join(", ") || "nothing"}, none of which is a search-result provider`,
        ).toBeGreaterThan(0);
    });

    test("an article seeded from Wikipedia renders on the Article tab, not only through the API", async ({ campus, page }) => {
        const article = await waitForOrNull(() => campus.api.get(`pins/${campus.pin.slug}/article/`), (response) => response.status() === 200, {
            what: "an article on the campus pin",
            timeoutMs: 300_000,
            intervalMs: 15_000,
            describe: (response) => `HTTP ${response.status()}`,
        });
        test.skip(article === null, "no article on the pin - hrsh-pin-enrichment.spec.ts reports that as the finding.");

        await page.goto(pinPath(campus.pin.slug));
        await waitForHtmxSettled(page, 30_000);
        // The panel may already have loaded with the page, so wait for its editor rather than for a swap.
        await timed("hrsh.pin_page.article_reveal_ms", async () => {
            await page.locator('a[data-tab="article"]').click();
            await expect(page.locator("#article-panel [data-article-textarea]"), "the Article tab never rendered its article").toBeAttached({ timeout: 30_000 });
        });

        const content = (await page.locator("#article-panel [data-article-textarea]").inputValue()).toLowerCase();
        expect(content.length, "the Article tab revealed but its textarea is empty").toBeGreaterThan(0);
        expect(/hudson river state hospital|poughkeepsie/.test(content), "the rendered article mentions neither Hudson River State Hospital nor Poughkeepsie").toBe(true);
        expect(/wikipedia/.test(content), "the rendered article carries no Wikipedia attribution, which CC BY-SA requires").toBe(true);
    });

    test("the private pin page loads within a sane time budget", async ({ campus, page }) => {
        const load = await openPrivatePin(page, campus.pin.slug);
        expect(load.timings.domContentLoadedMs, "DOMContentLoaded never fired").not.toBeNull();

        // Leaflet, MapLibre GL, leaflet-draw and map-annotations all load before this page can
        // render; 10s is the ceiling already recorded for this metric in metrics-baseline.json.
        expect(load.timings.domContentLoadedMs!, `DOMContentLoaded took ${load.timings.domContentLoadedMs}ms`).toBeLessThan(10_000);
    });
});
