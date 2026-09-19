/**
 * What a viewport's worth of tiles costs the deployment serving them, when it cannot serve them.
 *
 * The retry schedule is there so a tile refused by a busy proxy is not a permanent hole in the map.
 * Its risk is the opposite case: when the deployment is refusing everything, retrying turns one
 * viewport into several viewports' worth of requests, and it does so exactly when the server is
 * least able to answer them. These are budgets on that, not on speed - the numbers are counts of
 * requests, which are the same on any machine.
 */
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { registerRedataLayers, resetRedataLayersCacheForTests, tileLayer } from "./map-layers";
import { fetchOwnTile, resetOwnTileGateForTests } from "./own-tiles";

/** Tiles in one cold viewport - the unit a user actually asks for. */
const VIEWPORT_TILES = 30;

const realFetch = globalThis.fetch;
const realSetTimeout = globalThis.setTimeout;

let scheduled: Array<{ fn: () => void; ms: number }>;

/** Answers every request the way a proxy with no upstream slots left does. */
function stubRefusingProxy(status = 503): { calls: number } {
    const state = { calls: 0 };
    globalThis.fetch = (() => {
        state.calls++;
        return Promise.resolve({
            ok: false,
            status,
            headers: { get: (name: string) => (name.toLowerCase() === "retry-after" ? "1" : null) },
            arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)),
        } as unknown as Response);
    }) as unknown as typeof fetch;
    return state;
}

/** Runs the backoff timers without waiting them out, so a schedule spanning ~26s takes no time. */
async function runOutTheSchedule(): Promise<void> {
    for (let round = 0; round < 60; round++) {
        const due = scheduled;
        scheduled = [];
        due.forEach((timer) => timer.fn());
        for (let turn = 0; turn < 6; turn++) await Promise.resolve();
    }
}

beforeEach(() => {
    resetOwnTileGateForTests();
    scheduled = [];
    globalThis.setTimeout = ((fn: () => void, ms: number) => {
        scheduled.push({ fn, ms });
        return 0;
    }) as unknown as typeof setTimeout;
});

afterEach(() => {
    globalThis.fetch = realFetch;
    globalThis.setTimeout = realSetTimeout;
    resetOwnTileGateForTests();
});

describe("a viewport asked of a deployment that cannot serve it", () => {
    async function askForAViewport(): Promise<void> {
        const tiles = Array.from({ length: VIEWPORT_TILES }, (_, index) =>
            fetchOwnTile(`/dashboard/map/basemap-tiles/street/12/${1200 + index}/1539/`).catch(() => null),
        );
        await runOutTheSchedule();
        await Promise.all(tiles);
    }

    /**
     * Without a shared answer, each tile spends its whole retry budget discovering on its own what
     * the tile before it already found out - and the server pays for every attempt.
     */
    test("costs at most one request per tile", async () => {
        const proxy = stubRefusingProxy();

        await askForAViewport();

        console.log(`\n  ${VIEWPORT_TILES} tiles against a refusing proxy: ${proxy.calls} requests`);
        expect(proxy.calls).toBeLessThanOrEqual(VIEWPORT_TILES);
    });

    /**
     * A map the user has moved on from must not keep the deployment busy proving it is still down.
     * Each viewport is its own tiles, so three of them cost three viewports - the cost that must not
     * appear is the retries, which is what the budget below is.
     */
    test("does not multiply when the user pans across several viewports", async () => {
        const proxy = stubRefusingProxy();

        await askForAViewport();
        const first = proxy.calls;
        await askForAViewport();
        await askForAViewport();

        console.log(`  three viewports: ${proxy.calls} requests (first alone: ${first})`);
        expect(proxy.calls).toBeLessThanOrEqual(VIEWPORT_TILES * 3);
    });

    /**
     * A 404 is a real gap in the layer, not evidence the deployment is unwell - a sparse layer
     * would otherwise switch its own retries off for every other layer on the page.
     */
    test("a layer with holes in it does not count as an outage", async () => {
        const proxy = stubRefusingProxy(404);

        await askForAViewport();

        expect(proxy.calls).toBe(VIEWPORT_TILES);
    });
});

/**
 * The same budget for the path that draws the main map. Leaflet asks for its tiles with an
 * `<img src>` rather than through `fetchOwnTile`, so it has its own retry loop and would otherwise
 * keep the amplification on the largest surface while the tests above pass.
 */
describe("a Leaflet viewport asked of a deployment that cannot serve it", () => {
    const PLACEHOLDER = "data:image/gif;base64,placeholder";
    const TEMPLATE = "/dashboard/map/basemap-tiles/street/{z}/{x}/{y}/";

    interface StubLayer {
        options: Record<string, unknown>;
        getTileUrl(coords: { x: number; y: number; z: number }): string;
    }
    type CreateTile = (this: StubLayer, coords: { x: number; y: number; z: number }, done: (error?: Error) => void) => HTMLElement;

    const realL = (globalThis as Record<string, unknown>).L;
    let createTile: CreateTile | null = null;
    let layer: StubLayer | null = null;

    /** Enough of Leaflet for `tileLayer()` to build its subclass and for that subclass to run. */
    function stubLeaflet(): void {
        const makeLayer = (url: string, options: Record<string, unknown>): StubLayer => {
            layer = {
                options,
                getTileUrl: (coords) => url.replace("{z}", String(coords.z)).replace("{x}", String(coords.x)).replace("{y}", String(coords.y)),
            };
            return layer;
        };
        (globalThis as Record<string, unknown>).L = {
            tileLayer: makeLayer,
            TileLayer: {
                extend: (proto: { createTile: CreateTile }) => {
                    createTile = proto.createTile;
                    return function (this: unknown, url: string, options: Record<string, unknown>) {
                        return makeLayer(url, options);
                    };
                },
            },
        };
    }

    /**
     * Draws a viewport's worth of tiles against a deployment that answers none of them, counting
     * what it was asked for. A request is an `src` assignment, so it is cleared once counted and
     * the tile is told the load failed, which is all the browser gives this path.
     */
    async function drawAViewport(): Promise<number> {
        const tiles = Array.from({ length: VIEWPORT_TILES }, (_, index) => {
            const tile = createTile!.call(layer!, { x: 1200 + index, y: 1539, z: 12 }, () => {}) as HTMLImageElement;
            document.body.appendChild(tile);
            return tile;
        });
        let requests = 0;
        for (let round = 0; round < 40; round++) {
            for (const tile of tiles) {
                const src = tile.getAttribute("src");
                if (!src || src === PLACEHOLDER) continue;
                requests++;
                tile.removeAttribute("src");
                tile.onerror?.(new Event("error"));
                await Promise.resolve();
                await Promise.resolve();
            }
            const due = scheduled;
            scheduled = [];
            due.forEach((timer) => timer.fn());
            for (let turn = 0; turn < 6; turn++) await Promise.resolve();
        }
        tiles.forEach((tile) => tile.remove());
        return requests;
    }

    beforeEach(async () => {
        resetRedataLayersCacheForTests();
        globalThis.fetch = (() =>
            Promise.resolve({
                ok: true,
                json: () => Promise.resolve({ layers: [{ id: "street", source_type: "raster", url_template: TEMPLATE, attribution: "Attr" }] }),
            } as Response)) as unknown as typeof fetch;
        await registerRedataLayers();
        stubLeaflet();
        tileLayer("street", { errorTileUrl: PLACEHOLDER });
    });

    afterEach(() => {
        (globalThis as Record<string, unknown>).L = realL;
        resetRedataLayersCacheForTests();
        createTile = null;
        layer = null;
    });

    test("costs at most one request per tile", async () => {
        const requests = await drawAViewport();

        console.log(`  ${VIEWPORT_TILES} Leaflet tiles against a refusing proxy: ${requests} requests`);
        expect(requests).toBeLessThanOrEqual(VIEWPORT_TILES);
    });

    /** Panning must not re-multiply, and a tile the map still wants is still asked for once. */
    test("a second viewport costs the same again, and no more", async () => {
        const first = await drawAViewport();

        const second = await drawAViewport();

        console.log(`  a second Leaflet viewport: ${second} requests (first: ${first})`);
        expect(second).toBeLessThanOrEqual(VIEWPORT_TILES);
        expect(second).toBeGreaterThan(0);
    });
});

describe("recovering", () => {
    test("a deployment that starts answering again is asked again", async () => {
        stubRefusingProxy();
        const tiles = Array.from({ length: VIEWPORT_TILES }, (_, index) =>
            fetchOwnTile(`/tiles/12/${index}/1539/`).catch(() => null),
        );
        await runOutTheSchedule();
        await Promise.all(tiles);

        let asked = 0;
        globalThis.fetch = (() => {
            asked++;
            return Promise.resolve({ ok: true, status: 200, arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)) } as Response);
        }) as unknown as typeof fetch;

        // The wait the breaker asks for, then a tile the map still wants.
        const pending = fetchOwnTile("/tiles/12/99/1539/").catch(() => null);
        await runOutTheSchedule();
        const result = await pending;

        expect(asked).toBeGreaterThan(0);
        expect(result).not.toBeNull();
    });
});
