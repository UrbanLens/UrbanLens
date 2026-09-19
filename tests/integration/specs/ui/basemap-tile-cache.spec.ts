/**
 * Whether a second look at ground this browser has already drawn costs the deployment anything.
 *
 * A viewport is ~30 tiles and every one is an authenticated request through the whole middleware
 * chain, on the request threads the rest of the site shares. A tile is immutable for its layer and
 * coordinate, so the only thing standing between a user panning back and forth and a tile request
 * per tile per pan is the proxy's `Cache-Control` - which is why this is checked against a real
 * browser rather than by asserting the header in a unit test. Measured on this deployment before
 * the header existed: 48 of 48 tiles went back to the network on a second visit.
 */

import type { Page } from "@playwright/test";

import { expect, test } from "../../lib/fixtures.js";
import { MapPage } from "../../lib/pages/map-page.js";

/** One tile as the browser's own timing records it. `transferSize` is 0 for anything it did not go out for. */
interface TileEntry {
    name: string;
    transferSize: number;
}

const TILE_PATH = "/basemap-tiles/";
const CATALOGUE_PATH = "/sources/";

test.describe("basemap tiles the browser has already fetched", () => {
    /** Every proxied tile this page has asked for, whether or not it reached the network. */
    async function tilesFetched(page: Page): Promise<TileEntry[]> {
        return page.evaluate(
            (paths: { tile: string; catalogue: string }) =>
                performance
                    .getEntriesByType("resource")
                    .filter((entry) => entry.name.includes(paths.tile) && !entry.name.endsWith(paths.catalogue))
                    .map((entry) => ({
                        name: new URL(entry.name).pathname,
                        transferSize: (entry as PerformanceResourceTiming).transferSize,
                    })),
            { tile: TILE_PATH, catalogue: CATALOGUE_PATH },
        );
    }

    test("are not asked for again on a second visit to the same ground", async ({ page }) => {
        const map = new MapPage(page);
        await map.goto();
        const first = await tilesFetched(page);

        test.skip(
            first.length === 0,
            "this deployment serves no basemap tiles of its own - the map is drawing a vendor's, which this proxy does not control",
        );

        const directives = await page.evaluate(
            async (path) => (await fetch(path, { headers: { Accept: "image/*" } })).headers.get("Cache-Control"),
            first[0]!.name,
        );
        expect(directives, "a tile a shared cache could keep is a tile served to the wrong viewer").toContain("private");
        expect(directives).toContain("max-age=");

        // A fresh load rather than a pan: Leaflet keeps its <img> elements across a zoom cycle, so
        // only a new document makes the browser decide about every tile again.
        await map.goto();
        const second = await tilesFetched(page);

        const alreadyHad = new Set(first.map((entry) => entry.name));
        const revisited = second.filter((entry) => alreadyHad.has(entry.name));
        const wentOut = revisited.filter((entry) => entry.transferSize > 0);

        expect(revisited.length, "the second visit drew different ground, so nothing here was measured").toBeGreaterThan(0);
        expect(wentOut.map((entry) => entry.name)).toEqual([]);
    });
});
