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
export const DEFAULT_BUDGETS_MS = { page: 1000, fragment: 500, bulk: null };

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
    pin_gallery: "fragment",
    pin_nearby: "fragment",
    pin_visits: "fragment",
    notifications_dropdown: "fragment",
    notifications_unread: "fragment",
    messages_list: "fragment",
    messages_unread: "fragment",
    search_panel: "fragment",
    safety_banner: "fragment",
    map_document: "bulk",
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
