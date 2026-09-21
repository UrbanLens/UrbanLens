/** `capacity.js` decides what a run measures; a wrong stage tag or a skewed draw is invisible in the result. */

import { describe, expect, test } from "bun:test";

import {
    ARRIVALS_PER_SECOND,
    DEFAULT_LEVELS,
    DRAIN_SECONDS,
    ENDPOINTS,
    JOURNEYS,
    MAX_THINK_SECONDS,
    MIN_RAMP_SECONDS,
    MIN_THINK_SECONDS,
    PREFLIGHT_PROBE_TILES,
    TILE_GRID_ORIGIN,
    TILE_GRID_SIZE,
    TILE_LAYER,
    TILE_ZOOM,
    VIEWPORT_TILES,
    accountIndex,
    autocompleteQueries,
    buildStages,
    buildThresholds,
    filterQueries,
    forwardedFor,
    gridProbeTiles,
    holds,
    k6Stages,
    mapLoadEndpoints,
    parseLevels,
    pickJourney,
    stageAt,
    mapVisitSeed,
    storeClaim,
    thinkSeconds,
    tilesToFetch,
    totalSeconds,
    viewportTiles,
} from "./capacity.js";

/** A seeded uniform source, so a distribution test cannot flake. */
function seeded(seed) {
    let state = seed >>> 0;
    return () => {
        state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
        return state / 4294967296;
    };
}

describe("the timeline", () => {
    test("alternates a ramp and a hold per level, then drains", () => {
        const stages = buildStages([100, 1000], 240);

        expect(stages.map((stage) => stage.kind)).toEqual(["ramp", "hold", "ramp", "hold", "drain"]);
        expect(stages.map((stage) => stage.users)).toEqual([100, 100, 1000, 1000, 0]);
        expect(stages[stages.length - 1].seconds).toBe(DRAIN_SECONDS);
    });

    test("starts are the running total of the durations", () => {
        const stages = buildStages();
        for (let index = 1; index < stages.length; index += 1) {
            expect(stages[index].start).toBe(stages[index - 1].start + stages[index - 1].seconds);
        }
        expect(totalSeconds(stages)).toBe(stages.reduce((sum, stage) => sum + stage.seconds, 0));
    });

    test("a large step ramps at the arrival rate rather than all at once", () => {
        const [first, , second] = buildStages([100, 1000], 60);

        expect(first.seconds).toBe(MIN_RAMP_SECONDS);
        expect(second.seconds).toBe(Math.ceil(900 / ARRIVALS_PER_SECOND));
    });

    test("k6 is handed the same timeline", () => {
        const stages = buildStages([10, 20], 30);

        expect(k6Stages(stages)).toEqual(stages.map((stage) => ({ duration: `${stage.seconds}s`, target: stage.users })));
    });

    test("every second is tagged with the stage that covers it", () => {
        const stages = buildStages([100, 250], 120);

        for (const stage of stages) {
            expect(stageAt(stage.start, stages)).toBe(stage.name);
            expect(stageAt(stage.start + stage.seconds - 0.001, stages)).toBe(stage.name);
        }
        expect(stageAt(totalSeconds(stages) + 60, stages)).toBe("drain");
    });

    test("levels that do not climb are refused", () => {
        expect(() => buildStages([100, 100])).toThrow();
        expect(() => buildStages([250, 100])).toThrow();
        expect(() => buildStages([])).toThrow();
        expect(() => buildStages([1.5])).toThrow();
        expect(() => buildStages([100], 0)).toThrow();
    });

    test("levels parse from the runner's flag, and blank is the default", () => {
        expect(parseLevels("50, 200,800")).toEqual([50, 200, 800]);
        expect(parseLevels("")).toEqual(DEFAULT_LEVELS);
        expect(() => parseLevels("100,lots")).toThrow();
    });
});

describe("what a user does", () => {
    test("think time is bounded and centred on the median", () => {
        const random = seeded(7);
        const samples = Array.from({ length: 20000 }, () => thinkSeconds(random, 30, 0.8)).sort((a, b) => a - b);

        expect(samples[0]).toBeGreaterThanOrEqual(MIN_THINK_SECONDS);
        expect(samples[samples.length - 1]).toBeLessThanOrEqual(MAX_THINK_SECONDS);
        expect(samples[samples.length / 2]).toBeGreaterThan(27);
        expect(samples[samples.length / 2]).toBeLessThan(33);
    });

    test("journeys are drawn in proportion to their weights", () => {
        const random = seeded(11);
        const draws = 50000;
        const counts = {};
        for (let index = 0; index < draws; index += 1) {
            const name = pickJourney(random);
            counts[name] = (counts[name] || 0) + 1;
        }
        const total = JOURNEYS.reduce((sum, journey) => sum + journey.weight, 0);

        for (const journey of JOURNEYS) {
            expect(Math.abs((counts[journey.name] || 0) / draws - journey.weight / total)).toBeLessThan(0.01);
        }
    });

    test("every VU gets its own address, outside every range the proxy trusts", () => {
        const seen = new Set();
        for (const vu of [...Array.from({ length: 3000 }, (_, index) => index + 1), 65536, 0x3fffff]) {
            const address = forwardedFor(vu);
            const octets = address.split(".").map(Number);
            expect(octets[0]).toBe(100);
            expect(octets[1]).toBeGreaterThanOrEqual(64);
            expect(octets[1]).toBeLessThanOrEqual(127);
            expect(octets.every((octet) => octet >= 0 && octet <= 255)).toBe(true);
            seen.add(address);
        }
        expect(seen.size).toBe(3002);
        expect(() => forwardedFor(0)).toThrow();
        expect(() => forwardedFor(0x400000)).toThrow();
    });

    test("VUs map onto accounts one to one until they run out", () => {
        expect(accountIndex(1, 1000)).toBe(0);
        expect(accountIndex(1000, 1000)).toBe(999);
        expect(accountIndex(1001, 1000)).toBe(0);
        expect(() => accountIndex(1, 0)).toThrow();
    });

    test("typing into the filter starts broad and narrows", () => {
        const [broad, ...narrower] = filterQueries("Perf Pin");

        expect(broad).toBe("Perf Pin");
        for (const query of narrower) {
            expect(query.startsWith(broad)).toBe(true);
        }
    });

    test("typing into the search box asks at each pause, long enough to be answered, narrowing as it goes", () => {
        const queries = autocompleteQueries("Perf Pin");

        expect(queries.length).toBeGreaterThan(1);
        for (const [index, query] of queries.entries()) {
            expect(query.length).toBeGreaterThanOrEqual(2);
            if (index > 0) {
                expect(query.startsWith(queries[index - 1])).toBe(true);
            }
        }
    });

    test("a cold map downloads every pin and then asks whether they changed", () => {
        expect(mapLoadEndpoints(true)).toEqual(["map_document", "map_pins_meta"]);
        expect(mapLoadEndpoints(false)).toEqual(["map_pins_meta"]);
    });

    test("a filter claims the store with the fingerprint meta served, and claims nothing it cannot read", () => {
        expect(storeClaim(JSON.stringify({ fingerprint: "f1", last_updated: "2026-09-15T00:00:00Z" }))).toBe("f1");
        expect(storeClaim(JSON.stringify({ last_updated: "2026-09-15T00:00:00Z" }))).toBe("");
        expect(storeClaim("<!doctype html>")).toBe("");
        expect(storeClaim(null)).toBe("");
    });
});

describe("the thresholds", () => {
    const stages = buildStages([100, 250], 60);

    test("every endpoint is aggregated in every hold, and nothing in a ramp", () => {
        const thresholds = buildThresholds(stages);

        for (const stage of holds(stages)) {
            for (const endpoint of Object.keys(ENDPOINTS)) {
                expect(thresholds[`http_req_duration{endpoint:${endpoint},stage:${stage.name}}`]).toBeDefined();
            }
        }
        expect(Object.keys(thresholds).some((key) => key.includes("stage:ramp_"))).toBe(false);
    });

    test("a budget class of null is recorded rather than asserted", () => {
        const thresholds = buildThresholds(stages, { page: 1000, fragment: 500, bulk: null });

        expect(thresholds["http_req_duration{endpoint:map_document,stage:u100}"]).toEqual(["p(95)>=0"]);
        expect(thresholds["http_req_duration{endpoint:map_view,stage:u100}"]).toEqual(["p(95)<1000"]);
        expect(thresholds["http_req_duration{endpoint:map_search,stage:u100}"]).toEqual(["p(95)<500"]);
    });

    test("a run without sockets does not assert on them", () => {
        expect(Object.keys(buildThresholds(stages, undefined, { sockets: false })).some((key) => key.startsWith("ws_"))).toBe(false);
        expect(buildThresholds(stages)["ws_handshake_ok{stage:u250}"]).toEqual(["rate>0.995"]);
    });

    test("page views are aggregated per hold, since k6 exports only the submetrics a threshold names", () => {
        const thresholds = buildThresholds(stages);

        for (const stage of holds(stages)) {
            expect(thresholds[`page_views{stage:${stage.name}}`]).toEqual(["count>=0"]);
        }
    });

    test("the sign-in guard is always asserted", () => {
        expect(buildThresholds(stages)["checks{guard:signed_in}"]).toEqual(["rate==1"]);
    });
});

describe("the viewport a map asks for", () => {
    test("is a whole viewport's worth of tiles", () => {
        expect(viewportTiles(1)).toHaveLength(VIEWPORT_TILES);
        expect(new Set(viewportTiles(1)).size).toBe(VIEWPORT_TILES);
    });

    test("stays inside the grid the deployment's cache was seeded for", () => {
        for (let seed = 0; seed < 5000; seed += 7) {
            for (const path of viewportTiles(seed)) {
                const match = /^\/dashboard\/map\/basemap-tiles\/([a-z]+)\/(\d+)\/(\d+)\/(\d+)\/$/.exec(path);
                expect(match, `${path} is not a tile path the proxy route can carry`).not.toBeNull();
                const [, layer, zoom, x, y] = match;
                expect(layer).toBe(TILE_LAYER);
                expect(Number(zoom)).toBe(TILE_ZOOM);
                expect(Number(x)).toBeGreaterThanOrEqual(TILE_GRID_ORIGIN.x);
                expect(Number(x)).toBeLessThan(TILE_GRID_ORIGIN.x + TILE_GRID_SIZE);
                expect(Number(y)).toBeGreaterThanOrEqual(TILE_GRID_ORIGIN.y);
                expect(Number(y)).toBeLessThan(TILE_GRID_ORIGIN.y + TILE_GRID_SIZE);
            }
        }
    });

    /** Every user staring at the same 24 tiles measures one cache entry, not a deployment. */
    test("puts different users on different ground", () => {
        const ground = new Set(Array.from({ length: 200 }, (_, index) => viewportTiles(index * 7).join("|")));

        expect(ground.size).toBeGreaterThan(50);
    });
});

describe("where a person's next map view lands", () => {
    /** How much of a session's tile load the browser absorbs, at a given revisit share. */
    function suppressed(revisitShare, visits = 12) {
        const held = new Set();
        let wanted = 0;
        let fetched = 0;
        for (let visit = 1; visit <= visits; visit++) {
            const tiles = viewportTiles(mapVisitSeed(1, visit, revisitShare));
            wanted += tiles.length;
            fetched += tilesToFetch(tiles, held).length;
        }
        return 1 - fetched / wanted;
    }

    /**
     * The defect this replaced: seeds one apart shift the viewport by a single column, so four
     * fifths of every revisit came out of the browser and the deployment was barely asked for
     * tiles - on the request a capacity run exists to count.
     */
    test("steps a whole viewport, so fresh ground is fresh", () => {
        const first = new Set(viewportTiles(mapVisitSeed(1, 1, 0)));
        const second = viewportTiles(mapVisitSeed(1, 2, 0));

        expect(second.filter((path) => first.has(path))).toHaveLength(0);
    });

    test("returns to ground already drawn on the share asked for, and the browser answers all of it", () => {
        expect(suppressed(1)).toBeGreaterThan(0.9);
        expect(suppressed(0.5)).toBeGreaterThan(suppressed(0));
        expect(suppressed(0.5)).toBeLessThan(suppressed(1));
    });

    /** A run is judged against another run; a revisit pattern that differed between them would move the tile count for no measured reason. */
    test("is the same sequence every run", () => {
        const once = Array.from({ length: 10 }, (_, visit) => mapVisitSeed(3, visit + 1, 0.5));
        const again = Array.from({ length: 10 }, (_, visit) => mapVisitSeed(3, visit + 1, 0.5));

        expect(once).toEqual(again);
    });

    /**
     * Not disjoint, and cannot be: the grid is 32x32 so the cache can be warmed before a run, and
     * a thousand people asking for 24 tiles each want more ground than that holds. What matters is
     * that they are not all staring at one square, which would measure one cache entry.
     */
    test("spreads accounts across the grid", () => {
        const first = new Set(viewportTiles(mapVisitSeed(1, 1, 0.5)));
        const second = viewportTiles(mapVisitSeed(2, 1, 0.5));

        expect(second.filter((path) => first.has(path)).length).toBeLessThan(second.length / 2);
    });
});

describe("what a browser goes out for", () => {
    test("is every tile it does not already hold", () => {
        const held = new Set();

        expect(tilesToFetch(viewportTiles(1), held)).toHaveLength(VIEWPORT_TILES);
        expect(held.size).toBe(VIEWPORT_TILES);
    });

    /** The tile proxy marks a tile immutable, so a second look at the same ground costs nothing. */
    test("is nothing at all on a second look at the same ground", () => {
        const held = new Set();
        tilesToFetch(viewportTiles(1), held);

        expect(tilesToFetch(viewportTiles(1), held)).toEqual([]);
    });

    test("is only the new part of a viewport the user panned into", () => {
        const held = new Set();
        tilesToFetch(viewportTiles(1), held);

        const panned = tilesToFetch(viewportTiles(2), held);

        expect(panned.length).toBeGreaterThan(0);
        expect(panned.length).toBeLessThanOrEqual(VIEWPORT_TILES);
    });
});

describe("the pre-flight probe", () => {
    /** One seeded cell passing for 1,024 is how X26's run measured 8,360 refusals as latency. */
    test("samples many distinct cells, not one", () => {
        const probes = gridProbeTiles();

        expect(probes).toHaveLength(PREFLIGHT_PROBE_TILES);
        expect(new Set(probes).size).toBe(PREFLIGHT_PROBE_TILES);
    });

    test("spreads over both axes rather than one row", () => {
        const coordinates = gridProbeTiles().map((path) => path.split("/").filter(Boolean).slice(-2).map(Number));
        const columns = new Set(coordinates.map(([x]) => x));
        const rows = new Set(coordinates.map(([, y]) => y));

        expect(columns.size).toBe(PREFLIGHT_PROBE_TILES);
        expect(rows.size).toBe(PREFLIGHT_PROBE_TILES);
    });

    test("stays inside the grid the runner seeds", () => {
        for (const path of gridProbeTiles(TILE_GRID_SIZE * 4)) {
            const [x, y] = path.split("/").filter(Boolean).slice(-2).map(Number);

            expect(x).toBeGreaterThanOrEqual(TILE_GRID_ORIGIN.x);
            expect(x).toBeLessThan(TILE_GRID_ORIGIN.x + TILE_GRID_SIZE);
            expect(y).toBeGreaterThanOrEqual(TILE_GRID_ORIGIN.y);
            expect(y).toBeLessThan(TILE_GRID_ORIGIN.y + TILE_GRID_SIZE);
        }
    });
});
