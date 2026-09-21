/**
 * How many signed-in people one deployment serves at once.
 *
 * Every VU is one person from a provisioned population: it signs in with a minted session, browses the pages an
 * ordinary account uses with think time between them, keeps the header polls the pages run, and holds the
 * notification socket every page opens. Levels climb until the target, and each hold is judged on its own.
 */

import exec from "k6/execution";
import http from "k6/http";
import { check, fail, sleep } from "k6";
import { Counter, Rate } from "k6/metrics";
import { clearInterval, clearTimeout, setInterval, setTimeout } from "k6/timers";
import { WebSocket } from "k6/websockets";

import {
    DEFAULT_BUDGETS_MS,
    DEFAULT_HOLD_SECONDS,
    DEFAULT_THINK_MEDIAN_SECONDS,
    DEFAULT_THINK_SIGMA,
    ENDPOINTS,
    PREFLIGHT_PROBE_TILES,
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
    storeClaim,
    thinkSeconds,
    tilesToFetch,
    totalSeconds,
    viewportTiles,
} from "./lib/capacity.js";
import { adopt, get, getParams, postForm } from "./lib/session.js";

const BASE_URL = (__ENV.UL_PERF_BASE_URL || "").replace(/\/+$/, "");
if (!BASE_URL) {
    throw new Error("UL_PERF_BASE_URL is required. Run this through bin/run_capacity_tests.sh.");
}
const WS_BASE_URL = BASE_URL.replace(/^http/, "ws");

const MANIFEST = JSON.parse(open(__ENV.UL_PERF_MANIFEST || "/manifest.json"));
const ACCOUNTS = MANIFEST.accounts || [];
const ROUTES = MANIFEST.routes || {};

const LOGIN_PATH = "/accounts/login";
const STAGES = buildStages(parseLevels(__ENV.UL_CAP_LEVELS), Number(__ENV.UL_CAP_HOLD_SECONDS || DEFAULT_HOLD_SECONDS));
const TARGET_USERS = Math.max(...holds(STAGES).map((stage) => stage.users));
const THINK_MEDIAN = Number(__ENV.UL_CAP_THINK_MEDIAN_SECONDS || DEFAULT_THINK_MEDIAN_SECONDS);
const THINK_SIGMA = Number(__ENV.UL_CAP_THINK_SIGMA || DEFAULT_THINK_SIGMA);

/** Share of users whose first map visit finds no pin cache in the browser and downloads the whole document. */
const COLD_CACHE_SHARE = Number(__ENV.UL_CAP_COLD_CACHE_SHARE || 0.3);

/** Share of map visits that go on to type into the name filter. */
const FILTER_SHARE = Number(__ENV.UL_CAP_FILTER_SHARE || 0.4);

/** Share of map visits that type a pin's name into the search box. Assumed, not observed. */
const SEARCH_BOX_SHARE = Number(__ENV.UL_CAP_SEARCH_BOX_SHARE || 0.25);

/** Share of filter sessions run with the pin list open, which refetches the list when the filter commits. Assumed, not observed. */
const SIDEBAR_SHARE = Number(__ENV.UL_CAP_SIDEBAR_SHARE || 0.5);

/** Share of page views that open the notification bell. */
const BELL_SHARE = 0.1;

const SOCKETS = (__ENV.UL_CAP_SOCKETS || "1") !== "0";

/**
 * Whether a map visit draws its tiles. Off measures the same journey without the site's most
 * numerous request, so what tiles cost the whole deployment is a difference between two runs an
 * hour apart rather than a comparison across whatever else changed between two dates.
 */
const TILES = (__ENV.UL_CAP_TILES || "1") !== "0";
const BUDGETS = {
    page: Number(__ENV.UL_CAP_PAGE_BUDGET_MS || DEFAULT_BUDGETS_MS.page),
    fragment: Number(__ENV.UL_CAP_FRAGMENT_BUDGET_MS || DEFAULT_BUDGETS_MS.fragment),
    bulk: null,
    tile: Number(__ENV.UL_CAP_TILE_BUDGET_MS || DEFAULT_BUDGETS_MS.tile),
};
const SUMMARY_PATH = __ENV.UL_PERF_SUMMARY || "";
const STAGES_PATH = __ENV.UL_CAP_STAGES_PATH || "";

/** The browser's own ceiling on the map document, and generous for everything else. */
const REQUEST_TIMEOUT = "120s";

const HEARTBEAT_MS = 45000;
const HEARTBEAT_FRAME = JSON.stringify({ type: "ping" });
const UNREAD_POLL_SECONDS = 60;
const MAP_META_POLL_SECONDS = 120;
const SOCKET_BACKOFF_MAX_SECONDS = 30;

const wsHandshakeOk = new Rate("ws_handshake_ok");
const wsDropped = new Counter("ws_dropped");
const pageViews = new Counter("page_views");

export const options = {
    scenarios: buildScenarios(),
    thresholds: buildThresholds(STAGES, BUDGETS, { sockets: SOCKETS }),
    // Bodies are read for timing and thrown away; a thousand VUs holding map documents would OOM the generator.
    discardResponseBodies: true,
    summaryTrendStats: ["min", "med", "avg", "p(90)", "p(95)", "p(99)", "max", "count"],
    setupTimeout: "120s",
    userAgent: "UrbanLens-capacity/1 (k6)",
};

function buildScenarios() {
    const ramp = { executor: "ramping-vus", startVUs: 0, stages: k6Stages(STAGES), gracefulRampDown: "30s", gracefulStop: "30s" };
    const scenarios = { browsing: Object.assign({ exec: "browse" }, ramp) };
    if (SOCKETS) {
        scenarios.sockets = Object.assign({ exec: "notificationSocket" }, ramp);
    }
    return scenarios;
}

/** Refuse a run that would measure the wrong thing, before spending half an hour on it. */
export function setup() {
    if (MANIFEST.kind !== "population") {
        fail("The manifest is not a population. Provision one with provision_integration_env --population N.");
    }
    if (ACCOUNTS.length < TARGET_USERS && __ENV.UL_CAP_ALLOW_SHARED_ACCOUNTS !== "1") {
        fail(`${TARGET_USERS} users need ${TARGET_USERS} accounts and the manifest holds ${ACCOUNTS.length}. Accounts shared between VUs share caches and sockets, which flatters the result.`);
    }
    for (const name of ["map.view", "map.document", "map.pins.meta", "map.search", "map.autocomplete.local", "map.pins.list", "messages.unread_count", "notifications.unread_count", "safety.active_banner"]) {
        if (!ROUTES[name]) {
            fail(`The manifest resolves no route named ${name}.`);
        }
    }
    const account = ACCOUNTS[0];
    if (!account.cookies || !account.cookies.sessionid || !account.cookies.csrftoken) {
        fail("Manifest cookies are not named sessionid and csrftoken, which is what lib/session.js installs.");
    }
    const session = adopt(BASE_URL, "population", account.cookies);
    const response = get(session, ROUTES["map.view"], { endpoint: "preflight" }, { redirects: 0 });
    if (response.status !== 200) {
        fail(`Pre-flight GET ${ROUTES["map.view"]} as ${account.username} answered ${response.status}; a minted session that is not signed in measures the sign-in page.`);
    }
    // A run that measures 30,000 misses looks like a fast deployment, so a wrong layer, an unseeded
    // cache or a proxy this deployment cannot serve has to be a refusal to start rather than a
    // result. Sampled across the grid rather than at one coordinate: X26's run had exactly one cell
    // hand-seeded, so a single-tile guard passed and the other 1,020 answered 503.
    if (TILES) {
        const cold = gridProbeTiles()
            .map((path) => ({ path, status: get(session, path, { endpoint: "preflight" }, { redirects: 0 }).status }))
            .filter((probe) => probe.status !== 200);
        if (cold.length) {
            fail(
                `Pre-flight: ${cold.length} of ${PREFLIGHT_PROBE_TILES} sampled tiles are not served (e.g. ${cold[0].path} answered ${cold[0].status}). ` +
                    "Seed the deployment's tile cache for the grid in lib/capacity.js before measuring, or this run measures misses.",
            );
        }
    }
    const tilesDrawn = TILES ? `${VIEWPORT_TILES} tiles/viewport` : "no tiles (UL_CAP_TILES=0)";
    console.log(`pre-flight ok: ${ACCOUNTS.length} accounts, ${MANIFEST.pins} pins, ${tilesDrawn}, levels ${holds(STAGES).map((stage) => stage.users).join(",")}, ${totalSeconds(STAGES)}s`);
    return { startedAtMs: Date.now() };
}

// -- one person ---------------------------------------------------------------

/** Per VU: each VU is its own runtime, so this is one person's browser. */
let person = null;

function me() {
    if (person) {
        return person;
    }
    const id = exec.vu.idInTest;
    const account = ACCOUNTS[accountIndex(id, ACCOUNTS.length)];
    const session = adopt(BASE_URL, "population", account.cookies);
    session.headers = { "X-Forwarded-For": forwardedFor(id) };
    // `heldTiles` is this browser's tile cache: the proxy marks a tile immutable, so one it has
    // already drawn is not asked for again however often the user comes back to that ground.
    person = { id, account, session, address: forwardedFor(id), mapVisited: false, socketFailures: 0, heldTiles: new Set(), mapVisits: 0 };
    return person;
}

function stageNow() {
    return stageAt(exec.instance.currentTestRunDuration / 1000, STAGES);
}

function signedIn(response) {
    check(response, { "still signed in": (r) => !String(r.url).includes(LOGIN_PATH) }, { guard: "signed_in" });
}

function fetchOne(state, endpoint, path, extra) {
    const response = get(state.session, path, { endpoint, stage: stageNow() }, Object.assign({ timeout: REQUEST_TIMEOUT }, extra));
    signedIn(response);
    return response;
}

/** Requests a page makes together, sent together the way a browser does. */
function fetchTogether(state, requests) {
    const stage = stageNow();
    const responses = http.batch(
        requests.map(([endpoint, path]) => ({
            method: "GET",
            url: `${BASE_URL}${path}`,
            params: getParams(state.session, { endpoint, stage }, { timeout: REQUEST_TIMEOUT }),
        })),
    );
    responses.forEach(signedIn);
    return responses;
}

/**
 * The tiles the map draws, asked for the way a browser asks: all at once, and only for the ground
 * this browser is not already holding.
 *
 * This is the numerous request on the site - a page is one request and its map is this many more -
 * and it is the one a deployment's request threads are actually spent on.
 */
function drawViewport(state) {
    if (!TILES) {
        return;
    }
    state.mapVisits += 1;
    // A different square each visit, so a returning user is panning rather than staring: the visit
    // number moves the viewport, and the account keeps them away from everyone else's ground.
    const wanted = viewportTiles(state.id * 7 + state.mapVisits);
    const missing = tilesToFetch(wanted, state.heldTiles);
    if (!missing.length) {
        return;
    }
    fetchTogether(
        state,
        missing.map((path) => ["basemap_tile", path]),
    );
}

/** A full page load, then what that page fetches as it renders. The header's badges arrive with the page. */
function page(state, endpoint, path, alongside) {
    fetchOne(state, endpoint, path);
    if (alongside && alongside.length) {
        fetchTogether(state, alongside);
    }
}

const JOURNEY_STEPS = {
    map(state) {
        const cold = !state.mapVisited && Math.random() < COLD_CACHE_SHARE;
        state.mapVisited = true;
        fetchOne(state, "map_view", ROUTES["map.view"]);
        drawViewport(state);
        let claim = "";
        for (const endpoint of mapLoadEndpoints(cold)) {
            if (endpoint === "map_pins_meta") {
                claim = storeClaim(fetchOne(state, endpoint, ROUTES["map.pins.meta"], { responseType: "text" }).body);
            } else {
                fetchOne(state, endpoint, ROUTES["map.document"]);
            }
        }
        const prefix = MANIFEST.pin_name_prefix || "Perf Pin";
        if (Math.random() < SEARCH_BOX_SHARE) {
            for (const query of autocompleteQueries(prefix)) {
                fetchOne(state, "map_autocomplete", `${ROUTES["map.autocomplete.local"]}?q=${encodeURIComponent(query)}`);
                sleep(0.4 + Math.random() * 0.8);
            }
        }
        if (Math.random() < FILTER_SHARE) {
            let committed = prefix;
            for (const query of filterQueries(prefix)) {
                const body = { name: query, store_fingerprint: claim };
                const response = postForm(state.session, ROUTES["map.search"], body, { endpoint: "map_search", stage: stageNow() }, { timeout: REQUEST_TIMEOUT });
                signedIn(response);
                committed = query;
                sleep(0.7 + Math.random() * 1.3);
            }
            if (Math.random() < SIDEBAR_SHARE) {
                fetchOne(state, "map_pins_list", `${ROUTES["map.pins.list"]}?name=${encodeURIComponent(committed)}&page_size=25`);
            }
        }
    },
    pin(state) {
        const paths = state.account.paths;
        if (!paths.pin) {
            return JOURNEY_STEPS.home(state);
        }
        page(state, "pin_details", paths.pin, [
            ["pin_gallery", paths.pin_gallery],
            ["pin_nearby", paths.pin_nearby],
        ]);
    },
    pin_visits(state) {
        if (state.account.paths.pin_visits) {
            fetchOne(state, "pin_visits", state.account.paths.pin_visits);
        }
    },
    home: (state) => page(state, "home_view", ROUTES["home.view"]),
    notifications: (state) => page(state, "notifications_view", ROUTES["notifications.view"]),
    messages: (state) => page(state, "messages_view", ROUTES["messages.view"], [["messages_list", ROUTES["messages.list"]]]),
    conversation(state) {
        const path = state.account.paths.conversation;
        return path ? page(state, "conversation", path) : JOURNEY_STEPS.messages(state);
    },
    wiki(state) {
        const path = state.account.paths.wiki;
        return path ? page(state, "wiki", path) : JOURNEY_STEPS.home(state);
    },
    organize: (state) => page(state, "organize_index", ROUTES["organize.index"]),
    memories: (state) => page(state, "memories_view", ROUTES["memories.view"]),
    trips: (state) => page(state, "trips_overview", ROUTES["trips.overview"]),
    profile: (state) => page(state, "profile_view", ROUTES["profile.view"]),
    friend_profile(state) {
        const path = state.account.paths.friend_profile;
        return path ? page(state, "friend_profile", path) : JOURNEY_STEPS.profile(state);
    },
    search(state) {
        // The dialog opens on the current page: suggestions first, then results as the user types.
        fetchOne(state, "search_panel", ROUTES["search.panel"]);
        sleep(1 + Math.random() * 2);
        fetchOne(state, "search_panel", `${ROUTES["search.panel"]}?q=${encodeURIComponent((MANIFEST.pin_name_prefix || "Perf Pin").split(" ")[0])}`);
    },
};

/** Stay on the page, running the polls a page left open runs. */
function think(state, journey) {
    let remaining = thinkSeconds(Math.random, THINK_MEDIAN, THINK_SIGMA);
    let untilUnread = UNREAD_POLL_SECONDS;
    let untilMeta = journey === "map" ? MAP_META_POLL_SECONDS : Infinity;
    if (Math.random() < BELL_SHARE) {
        fetchOne(state, "notifications_dropdown", ROUTES["notifications.dropdown"]);
    }
    while (remaining > 0) {
        const step = Math.min(remaining, untilUnread, untilMeta);
        sleep(step);
        remaining -= step;
        untilUnread -= step;
        untilMeta -= step;
        if (remaining <= 0) {
            break;
        }
        if (untilUnread <= 0) {
            fetchOne(state, "messages_unread", ROUTES["messages.unread_count"]);
            untilUnread = UNREAD_POLL_SECONDS;
        }
        if (untilMeta <= 0) {
            fetchOne(state, "map_pins_meta", ROUTES["map.pins.meta"]);
            untilMeta = MAP_META_POLL_SECONDS;
        }
    }
}

export function browse() {
    const state = me();
    const journey = pickJourney(Math.random);
    pageViews.add(1, { journey, stage: stageNow() });
    JOURNEY_STEPS[journey](state);
    think(state, journey);
}

/**
 * The notification socket, opened by a page load and closed by the next.
 *
 * Held for one think time and reopened, so the handshake rate follows the page-view rate the way it does in a
 * browser. A refused handshake backs off the way `_notification_push.html` does rather than retrying in a loop.
 */
export function notificationSocket() {
    const state = me();
    if (state.socketFailures > 0) {
        sleep(Math.min(SOCKET_BACKOFF_MAX_SECONDS, 2 ** (state.socketFailures - 1)));
    }
    const stage = stageNow();
    const holdMs = thinkSeconds(Math.random, THINK_MEDIAN, THINK_SIGMA) * 1000;
    const cookies = state.account.cookies;
    const socket = new WebSocket(`${WS_BASE_URL}/ws/notifications/`, null, {
        headers: {
            Cookie: `sessionid=${cookies.sessionid}; csrftoken=${cookies.csrftoken}`,
            Origin: BASE_URL,
            "X-Forwarded-For": state.address,
        },
        tags: { endpoint: "ws_notifications", stage },
    });
    let opened = false;
    let settled = false;
    let finishing = false;
    let heartbeat = null;
    let closer = null;

    const settle = (ok) => {
        if (!settled) {
            settled = true;
            wsHandshakeOk.add(ok ? 1 : 0, { stage });
            state.socketFailures = ok ? 0 : state.socketFailures + 1;
        }
    };

    socket.onopen = () => {
        opened = true;
        settle(true);
        heartbeat = setInterval(() => socket.send(HEARTBEAT_FRAME), HEARTBEAT_MS);
        closer = setTimeout(() => {
            finishing = true;
            socket.close();
        }, holdMs);
    };
    socket.onerror = () => {
        if (!opened) {
            settle(false);
        }
    };
    socket.onclose = (event) => {
        if (heartbeat !== null) {
            clearInterval(heartbeat);
        }
        if (closer !== null) {
            clearTimeout(closer);
        }
        const code = event && event.code !== undefined ? String(event.code) : "unknown";
        if (!opened) {
            settle(false);
        } else if (!finishing) {
            wsDropped.add(1, { stage: stageNow(), code });
        }
        check(code, { "still signed in": (value) => value !== "4404" }, { guard: "signed_in" });
    };
}

// -- verdict ------------------------------------------------------------------

export function handleSummary(data) {
    const output = { stdout: renderVerdict(data) };
    if (SUMMARY_PATH) {
        output[SUMMARY_PATH] = JSON.stringify(data, null, 2);
    }
    if (STAGES_PATH) {
        const startedAtMs = data.setup_data ? data.setup_data.startedAtMs : null;
        output[STAGES_PATH] = JSON.stringify({ started_at_ms: startedAtMs, stages: STAGES, budgets: BUDGETS, endpoints: ENDPOINTS }, null, 2);
    }
    return output;
}

function renderVerdict(data) {
    const lines = ["", `capacity run: levels ${holds(STAGES).map((stage) => stage.users).join(" -> ")}, page p95 < ${BUDGETS.page}ms, fragment p95 < ${BUDGETS.fragment}ms, tile p95 < ${BUDGETS.tile}ms`, ""];
    lines.push("  hold        failed    ws ok   worst page p95            worst fragment p95         tile p95   tiles     map_document p95");
    for (const stage of holds(STAGES)) {
        const failed = rate(data, `http_req_failed{stage:${stage.name}}`);
        const ws = rate(data, `ws_handshake_ok{stage:${stage.name}}`);
        const worst = (budgetClass) => {
            let found = null;
            for (const [endpoint, klass] of Object.entries(ENDPOINTS)) {
                const p95 = klass === budgetClass ? value(data, `http_req_duration{endpoint:${endpoint},stage:${stage.name}}`, "p(95)") : undefined;
                if (p95 !== undefined && (found === null || p95 > found.p95)) {
                    found = { endpoint, p95 };
                }
            }
            return found ? `${found.p95.toFixed(0)}ms ${found.endpoint}` : "-";
        };
        const documentP95 = value(data, `http_req_duration{endpoint:map_document,stage:${stage.name}}`, "p(95)");
        const tileP95 = value(data, `http_req_duration{endpoint:basemap_tile,stage:${stage.name}}`, "p(95)");
        const tileCount = value(data, `http_req_duration{endpoint:basemap_tile,stage:${stage.name}}`, "count");
        lines.push(
            `  ${stage.name.padEnd(10)} ${percent(failed).padStart(7)} ${percent(ws).padStart(8)}   ${worst("page").padEnd(25)} ${worst("fragment").padEnd(26)} ${(tileP95 === undefined ? "-" : `${tileP95.toFixed(0)}ms`).padEnd(10)} ${(tileCount === undefined ? "-" : String(tileCount)).padEnd(9)} ${documentP95 === undefined ? "-" : `${documentP95.toFixed(0)}ms`}`,
        );
    }
    const dropped = data.metrics.ws_dropped;
    if (dropped && dropped.values.count > 0) {
        lines.push("", `  ${dropped.values.count} open notification sockets were closed by the server or the proxy.`);
    }
    lines.push("");
    return lines.join("\n");
}

function value(data, key, stat) {
    const metric = data.metrics[key];
    return metric && metric.values ? metric.values[stat] : undefined;
}

/** A rate, or undefined when nothing was recorded: k6 reports an empty sub-metric as 0. */
function rate(data, key) {
    const metric = data.metrics[key];
    if (!metric || !metric.values || metric.values.passes + metric.values.fails === 0) {
        return undefined;
    }
    return metric.values.rate;
}

function percent(share) {
    return share === undefined ? "-" : `${(share * 100).toFixed(2)}%`;
}
