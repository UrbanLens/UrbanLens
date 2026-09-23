/**
 * Whether a second look at ground this browser has already drawn costs the deployment anything.
 *
 * A viewport is ~30 tiles and every one is an authenticated request through the whole middleware
 * chain, on the request threads the rest of the site shares. A tile is immutable for its layer and
 * coordinate, so the only thing standing between a user moving around the map and a tile request
 * per tile per move is the proxy's `Cache-Control` - which is why this is checked against a real
 * browser rather than by asserting the header in a unit test. Measured on this deployment before
 * the header existed: 48 of 48 tiles went back to the network on a second visit.
 *
 * Three ways back to the same ground are covered, because they fail differently. A zoom cycle and a
 * pan are the same document, where Leaflet decides whether to keep an `<img>` at all; a reload is a
 * new document, where the browser decides whether to keep a response. Each asserts the same thing -
 * nothing already drawn reaches the network a second time - so a Leaflet upgrade that starts
 * discarding tiles, or a swap to another map library, shows up here as a failure rather than as an
 * unexplained load on the tile proxy months later.
 */

import type { Page } from "@playwright/test";

import { expect, test } from "../../lib/fixtures.js";
import { MapPage } from "../../lib/pages/map-page.js";

/** Only what this spec needs of the live Leaflet map the page publishes for the console. */
interface LeafletMapHandle {
    getZoom(): number;
    setZoom(zoom: number, options?: { animate?: boolean }): void;
    getCenter(): { lat: number; lng: number };
    setView(center: [number, number], zoom: number, options?: { animate?: boolean }): void;
}

declare global {
    interface Window {
        map: LeafletMapHandle;
    }
}

/** One tile as the browser's own timing records it. `transferSize` is 0 for anything it did not go out for. */
interface TileEntry {
    name: string;
    transferSize: number;
}

const TILE_PATH = "/basemap-tiles/";
const CATALOGUE_PATH = "/sources/";

/**
 * Far enough that nothing of the first viewport is still on screen or in Leaflet's retain buffer:
 * ~23 tiles at zoom 13, against a viewport of ~6 and a buffer of 2 either side.
 */
const A_LONG_WAY_DEGREES = 1;

test.describe("basemap tiles the browser has already fetched", () => {
    // The buffer holds 250 entries by default and silently drops the rest, which for a spec that
    // moves the map around would quietly become "no tiles went out" - the result it is looking for.
    test.beforeEach(async ({ page }) => {
        await page.addInitScript(() => performance.setResourceTimingBufferSize(3000));
    });

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

    /**
     * Waits until the map has stopped asking for tiles.
     *
     * Counted from the browser's own timing rather than from `.leaflet-tile-loaded`, because a tile
     * the deployment could not serve never gets that class and would hang this instead.
     */
    async function settle(page: Page): Promise<TileEntry[]> {
        let previous = -1;
        let drawn: TileEntry[] = [];
        for (let round = 0; round < 40; round++) {
            drawn = await tilesFetched(page);
            // Only settled once something has actually been asked for. The map takes a moment to
            // start, and two zeroes in a row would otherwise read as "finished" - which skips these
            // tests rather than failing them, so nothing would ever say so.
            if (drawn.length > 0 && drawn.length === previous) return drawn;
            previous = drawn.length;
            await page.waitForTimeout(250);
        }
        return drawn;
    }

    /** Opens the map and returns what it drew, skipping the test when this deployment proxies no tiles of its own. */
    async function drawTheMap(page: Page): Promise<TileEntry[]> {
        const map = new MapPage(page);
        await map.goto();
        await settle(page);
        const drawn = await tilesFetched(page);

        test.skip(
            drawn.length === 0,
            "this deployment serves no basemap tiles of its own - the map is drawing a vendor's, which this proxy does not control",
        );
        // The measurement has to be able to tell a network fetch from a cached one, or every
        // assertion below passes for the wrong reason. A first sight of a tile is a fetch.
        expect(
            drawn.some((entry) => entry.transferSize > 0),
            "not one tile reports a transfer size, so this browser is not reporting what it fetched",
        ).toBe(true);
        return drawn;
    }

    /** The tiles this page asked for since `before` that it had already asked for once. */
    function askedForAgain(after: TileEntry[], before: TileEntry[]): TileEntry[] {
        const alreadyHad = new Set(before.map((entry) => entry.name));
        return after.slice(before.length).filter((entry) => alreadyHad.has(entry.name));
    }

    test("are not asked for again on a second visit to the same ground", async ({ page }) => {
        const first = await drawTheMap(page);

        const directives = await page.evaluate(
            async (path) => (await fetch(path, { headers: { Accept: "image/*" } })).headers.get("Cache-Control"),
            first[0]!.name,
        );
        // `public` on purpose (a4229e994, `_keep_for` in controllers/basemap_tiles.py): the bytes are
        // keyed on layer and coordinate alone, identical for every signed-in viewer, and `private`
        // kept every tile off the CDN and on a request thread.
        expect(directives, "a tile a CDN refuses to store is a tile the origin serves every time").toContain("public");
        expect(directives).not.toContain("private");
        expect(directives).toContain("max-age=");

        // A new document, so the browser decides about every tile again rather than reusing the
        // elements it already has.
        const map = new MapPage(page);
        await map.goto();
        await settle(page);
        const second = await tilesFetched(page);

        const alreadyHad = new Set(first.map((entry) => entry.name));
        const revisited = second.filter((entry) => alreadyHad.has(entry.name));
        const wentOut = revisited.filter((entry) => entry.transferSize > 0);

        expect(revisited.length, "the second visit drew different ground, so nothing here was measured").toBeGreaterThan(0);
        expect(wentOut.map((entry) => entry.name)).toEqual([]);
    });

    /**
     * Leaflet is expected to keep its `<img>` elements across a zoom cycle and ask for nothing at
     * all, which is why this asserts what reaches the network rather than what is requested: either
     * answer is fine, a network fetch is not. When written, it kept them.
     */
    test("are not asked for again after zooming out and back in", async ({ page }) => {
        const first = await drawTheMap(page);

        const zoomed = await page.evaluate(() => {
            const zoom = window.map.getZoom();
            window.map.setZoom(zoom - 1, { animate: false });
            return zoom;
        });
        await settle(page);

        const zoomedOut = await tilesFetched(page);
        expect(
            zoomedOut.length,
            "zooming out drew no tiles of its own, so the map did not follow the zoom and coming back proves nothing",
        ).toBeGreaterThan(first.length);

        await page.evaluate((zoom: number) => window.map.setZoom(zoom, { animate: false }), zoomed);
        await settle(page);
        expect(await page.evaluate(() => window.map.getZoom()), "the map did not come back to the zoom it started at").toBe(zoomed);

        const wentOut = askedForAgain(await tilesFetched(page), first).filter((entry) => entry.transferSize > 0);

        expect(wentOut.map((entry) => entry.name)).toEqual([]);
    });

    /**
     * Panning far enough drops tiles from the DOM - Leaflet keeps a couple of screens either side and
     * prunes the rest - so coming back is the case where the browser's own cache, and therefore the
     * proxy's header, is the only thing between a returning viewport and 30 fresh requests.
     */
    test("are not asked for again after panning away and back", async ({ page }) => {
        const first = await drawTheMap(page);

        const home = await page.evaluate((far: number) => {
            const centre = window.map.getCenter();
            const zoom = window.map.getZoom();
            window.map.setView([centre.lat + far, centre.lng + far], zoom, { animate: false });
            return { lat: centre.lat, lng: centre.lng, zoom };
        }, A_LONG_WAY_DEGREES);
        await settle(page);

        const awayFromHome = await tilesFetched(page);
        expect(
            awayFromHome.length,
            "panning drew no new tiles, so the map never left the ground it started on and coming back proves nothing",
        ).toBeGreaterThan(first.length);

        await page.evaluate(
            (start: { lat: number; lng: number; zoom: number }) => window.map.setView([start.lat, start.lng], start.zoom, { animate: false }),
            home,
        );
        await settle(page);

        const wentOut = askedForAgain(await tilesFetched(page), awayFromHome).filter((entry) => entry.transferSize > 0);

        expect(wentOut.map((entry) => entry.name)).toEqual([]);
    });
});
