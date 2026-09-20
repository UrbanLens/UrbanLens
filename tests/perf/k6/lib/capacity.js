/** The capacity run's timeline, and the decisions a virtual user makes that do not need k6 to check. */

/** Concurrent signed-in users at each hold. The last is the target the run is judged against. */
export const DEFAULT_LEVELS = [100, 250, 500, 1000];

/** Seconds each level is held. Long enough for several think-time cycles per user. */
export const DEFAULT_HOLD_SECONDS = 240;

/** New users let in per second while climbing to a level, so a ramp is arrivals rather than a stampede. */
export const ARRIVALS_PER_SECOND = 5;

export const MIN_RAMP_SECONDS = 60;

export const DRAIN_SECONDS = 30;

/** Median seconds a user spends on a page before the next, and the log-normal spread around it. */
export const DEFAULT_THINK_MEDIAN_SECONDS = 30;
export const DEFAULT_THINK_SIGMA = 0.8;
export const MIN_THINK_SECONDS = 3;
export const MAX_THINK_SECONDS = 600;

/** p95 ceilings, in milliseconds, per class of request. A class of `null` is recorded, never asserted. */
export const DEFAULT_BUDGETS_MS = { page: 1000, fragment: 500, bulk: null, tile: 300 };

/**
 * Basemap tiles one viewport asks for at once.
 *
 * A map is not one request, it is a page plus this many - so leaving them out of a capacity run
 * measures a deployment nobody uses. 24 is what a 1400x900 window actually asked this deployment
 * for, measured 2026-09-19; a taller window asks for more.
 */
export const VIEWPORT_TILES = 24;

/**
 * The layer a capacity run draws, and the zoom it draws it at. Both have to match whatever seeded
 * the cache.
 *
 * Not `street`: REData publishes that as vector (`D11`) and answers a tile-by-tile request for it
 * with 400 `vector_layer_not_served`, so nothing can fill that cache key from upstream any more. A
 * seeded run would not notice - `seed_basemap_tile_cache` writes placeholder bytes straight into
 * the cache, which is the point, so the layer is only a cache key there - but an unseeded or
 * expired one would measure 404s. `terrain` is still raster and goes through the identical view.
 * Override with `UL_CAP_TILE_LAYER`.
 */
// `__ENV` is k6's, and this module is also imported by the unit tests, which run under bun.
export const TILE_LAYER = (typeof __ENV === "undefined" ? "" : __ENV.UL_CAP_TILE_LAYER) || "terrain";
export const TILE_ZOOM = 13;

/**
 * The square of tile coordinates a run stays inside.
 *
 * Bounded so the deployment's own tile cache warms within the first few users rather than every
 * user paying an upstream fetch: the subject here is what this deployment costs to serve a tile it
 * already has, not what the vendor costs to fetch one. Wide enough that users are not all asking
 * for the same 24 tiles, which would measure one cache entry.
 */
export const TILE_GRID_ORIGIN = { x: 2400, y: 3072 };
export const TILE_GRID_SIZE = 32;

/** What a user does next, weighted by how often a page view is that page. */
export const JOURNEYS = [
    { name: "map", weight: 30 },
    { name: "pin", weight: 14 },
    { name: "home", weight: 8 },
    { name: "notifications", weight: 6 },
    { name: "messages", weight: 6 },
    { name: "conversation", weight: 5 },
    { name: "wiki", weight: 5 },
    { name: "organize", weight: 5 },
    { name: "search", weight: 5 },
    { name: "memories", weight: 4 },
    { name: "trips", weight: 4 },
    { name: "friend_profile", weight: 4 },
    { name: "profile", weight: 3 },
    { name: "pin_visits", weight: 1 },
];

/** Every request the scenario makes, by its `endpoint` tag, and the budget class it is judged under. */
export const ENDPOINTS = {
    map_view: "page",
    home_view: "page",
    pin_details: "page",
    wiki: "page",
    notifications_view: "page",
    messages_view: "page",
    conversation: "page",
    memories_view: "page",
    organize_index: "page",
    trips_overview: "page",
    profile_view: "page",
    friend_profile: "page",
    map_pins_meta: "fragment",
    map_search: "fragment",
    map_autocomplete: "fragment",
    map_pins_list: "fragment",
    pin_gallery: "fragment",
    pin_nearby: "fragment",
    pin_visits: "fragment",
    notifications_dropdown: "fragment",
    messages_list: "fragment",
    messages_unread: "fragment",
    search_panel: "fragment",
    map_document: "bulk",
    basemap_tile: "tile",
};

/**
 * The run as a list of ramps and holds.
 *
 * @param {number[]} levels Strictly increasing user counts.
 * @param {number} holdSeconds How long each is held.
 * @returns {{name: string, kind: string, users: number, start: number, seconds: number}[]}
 */
export function buildStages(levels = DEFAULT_LEVELS, holdSeconds = DEFAULT_HOLD_SECONDS) {
    if (!Array.isArray(levels) || levels.length === 0) {
        throw new Error("A capacity run needs at least one level.");
    }
    if (!(holdSeconds > 0)) {
        throw new Error(`A hold must last a positive number of seconds; got ${holdSeconds}.`);
    }
    const stages = [];
    let start = 0;
    let previous = 0;
    for (const users of levels) {
        if (!Number.isInteger(users) || users <= previous) {
            throw new Error(`Levels must be strictly increasing positive integers; got ${levels.join(",")}.`);
        }
        const ramp = Math.max(MIN_RAMP_SECONDS, Math.ceil((users - previous) / ARRIVALS_PER_SECOND));
        stages.push({ name: `ramp_${users}`, kind: "ramp", users, start, seconds: ramp });
        start += ramp;
        stages.push({ name: `u${users}`, kind: "hold", users, start, seconds: holdSeconds });
        start += holdSeconds;
        previous = users;
    }
    stages.push({ name: "drain", kind: "drain", users: 0, start, seconds: DRAIN_SECONDS });
    return stages;
}

/** `ramping-vus` stages for *stages*. */
export function k6Stages(stages) {
    return stages.map((stage) => ({ duration: `${stage.seconds}s`, target: stage.users }));
}

export function totalSeconds(stages) {
    const last = stages[stages.length - 1];
    return last.start + last.seconds;
}

/** The stage running *seconds* into the run. Past the end is still the drain. */
export function stageAt(seconds, stages) {
    for (let index = stages.length - 1; index >= 0; index -= 1) {
        if (seconds >= stages[index].start) {
            return stages[index].name;
        }
    }
    return stages[0].name;
}

export function holds(stages) {
    return stages.filter((stage) => stage.kind === "hold");
}

/** `"100,250"` as numbers. Blank means the default. */
export function parseLevels(text) {
    if (!text || !String(text).trim()) {
        return DEFAULT_LEVELS.slice();
    }
    return String(text)
        .split(",")
        .map((part) => part.trim())
        .filter((part) => part !== "")
        .map((part) => {
            if (!/^\d+$/.test(part)) {
                throw new Error(`Unreadable level: ${part}`);
            }
            return Number(part);
        });
}

/**
 * Seconds on a page, log-normally distributed: most views are short, a few are long.
 *
 * @param {() => number} random Uniform in [0, 1).
 */
export function thinkSeconds(random, median = DEFAULT_THINK_MEDIAN_SECONDS, sigma = DEFAULT_THINK_SIGMA) {
    // Box-Muller; 1 - random() keeps the logarithm finite.
    const normal = Math.sqrt(-2 * Math.log(1 - random())) * Math.cos(2 * Math.PI * random());
    const seconds = median * Math.exp(sigma * normal);
    return Math.min(MAX_THINK_SECONDS, Math.max(MIN_THINK_SECONDS, seconds));
}

/** A journey name, drawn by weight. */
export function pickJourney(random, journeys = JOURNEYS) {
    const total = journeys.reduce((sum, journey) => sum + journey.weight, 0);
    let draw = random() * total;
    for (const journey of journeys) {
        draw -= journey.weight;
        if (draw < 0) {
            return journey.name;
        }
    }
    return journeys[journeys.length - 1].name;
}

/**
 * A client address unique to one VU, for the proxy to key per-visitor limits on.
 *
 * Carrier-grade NAT space rather than a private range: the proxy trusts private ranges as its own hops, and an
 * address it trusts is skipped when it looks for the visitor.
 */
export function forwardedFor(vuId) {
    if (!Number.isInteger(vuId) || vuId < 1 || vuId > 0x3fffff) {
        throw new Error(`No address for VU ${vuId}.`);
    }
    return `100.${64 + ((vuId >> 16) & 63)}.${(vuId >> 8) & 255}.${vuId & 255}`;
}

/** Which manifest account a VU signs in as. Stable for the VU's life. */
export function accountIndex(vuId, count) {
    if (!(count > 0)) {
        throw new Error("The manifest holds no accounts.");
    }
    return (vuId - 1) % count;
}

/** What a user types into the map's name filter, one debounced request per entry. The first matches every seeded pin. */
export function filterQueries(pinNamePrefix) {
    return [pinNamePrefix, `${pinNamePrefix} 1`, `${pinNamePrefix} 12`];
}

/** What the map's search box sends as someone types a pin's name: one request per 400 ms pause, never under two characters. */
export function autocompleteQueries(pinNamePrefix) {
    const word = pinNamePrefix.split(" ")[0];
    return [word.length >= 2 ? word : pinNamePrefix, pinNamePrefix, `${pinNamePrefix} 1`];
}

/** What the map page fetches after its HTML, in order: with no pin cache it downloads every pin before polling meta. */
export function mapLoadEndpoints(cold) {
    return cold ? ["map_document", "map_pins_meta"] : ["map_pins_meta"];
}

/**
 * The tiles one viewport asks for, as paths.
 *
 * A viewport is a square of tiles around wherever the user is looking, so this walks out from a
 * centre the same way Leaflet does. Coordinates wrap inside the grid rather than running off it, so
 * every path a run produces is one the cache was seeded for.
 *
 * @param {number} seed Anything stable per user and per visit; decides which ground they look at.
 * @param {number} count How many tiles the viewport holds.
 * @returns {string[]} Tile paths, without the deployment's origin.
 */
export function viewportTiles(seed, count = VIEWPORT_TILES) {
    const side = Math.ceil(Math.sqrt(count));
    const centreX = TILE_GRID_ORIGIN.x + (Math.abs(Math.floor(seed)) % TILE_GRID_SIZE);
    const centreY = TILE_GRID_ORIGIN.y + (Math.abs(Math.floor(seed / TILE_GRID_SIZE)) % TILE_GRID_SIZE);
    const paths = [];
    for (let row = 0; row < side && paths.length < count; row++) {
        for (let column = 0; column < side && paths.length < count; column++) {
            const x = TILE_GRID_ORIGIN.x + (((centreX - TILE_GRID_ORIGIN.x + column) % TILE_GRID_SIZE) + TILE_GRID_SIZE) % TILE_GRID_SIZE;
            const y = TILE_GRID_ORIGIN.y + (((centreY - TILE_GRID_ORIGIN.y + row) % TILE_GRID_SIZE) + TILE_GRID_SIZE) % TILE_GRID_SIZE;
            paths.push(`/dashboard/map/basemap-tiles/${TILE_LAYER}/${TILE_ZOOM}/${x}/${y}/`);
        }
    }
    return paths;
}

/**
 * The tiles a browser would actually go out for, given what it is already holding.
 *
 * The proxy sends `Cache-Control: private, max-age, immutable`, so a tile this browser has drawn
 * before costs the deployment nothing on the next visit. A capacity run that re-asks for all of
 * them measures a deployment without that header - and one that never re-visits ground measures a
 * user who never pans back.
 *
 * @param {string[]} wanted Every tile in the viewport.
 * @param {Set<string>} held What this browser has already fetched, added to in place.
 * @returns {string[]} The subset that reaches the deployment.
 */
export function tilesToFetch(wanted, held) {
    const missing = wanted.filter((path) => !held.has(path));
    missing.forEach((path) => held.add(path));
    return missing;
}

/** The fingerprint a filter sends to claim the page's pin store, read from a meta response. Empty asks for payloads. */
export function storeClaim(metaBody) {
    try {
        const data = JSON.parse(metaBody);
        return data && typeof data.fingerprint === "string" ? data.fingerprint : "";
    } catch (error) {
        return "";
    }
}

/**
 * Thresholds that name every endpoint in every hold, which is the only way k6 aggregates a sub-metric, plus the
 * asserted ones.
 */
export function buildThresholds(stages, budgets = DEFAULT_BUDGETS_MS, { sockets = true } = {}) {
    const thresholds = {
        "checks{guard:signed_in}": ["rate==1"],
    };
    for (const stage of holds(stages)) {
        thresholds[`http_req_failed{stage:${stage.name}}`] = ["rate<0.005"];
        thresholds[`page_views{stage:${stage.name}}`] = ["count>=0"];
        if (sockets) {
            thresholds[`ws_handshake_ok{stage:${stage.name}}`] = ["rate>0.995"];
        }
        for (const [endpoint, budgetClass] of Object.entries(ENDPOINTS)) {
            const ceiling = budgets[budgetClass];
            thresholds[`http_req_duration{endpoint:${endpoint},stage:${stage.name}}`] = [ceiling ? `p(95)<${ceiling}` : "p(95)>=0"];
        }
    }
    return thresholds;
}
