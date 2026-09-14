/** The direct test of "no action a user takes should impact the availability of the site for other users, ever". One account (`heavy`) does progressively more expensive things. */

import exec from "k6/execution";
import { check, fail } from "k6";

import { ACTIONS } from "./lib/actions.js";
import { assertedPhases, baselinePhase, phaseAt, phaseStarts, selectPhases, totalSeconds } from "./lib/schedule.js";
import { adopt, get, signIn } from "./lib/session.js";

const BASE_URL = (__ENV.UL_PERF_BASE_URL || "").replace(/\/+$/, "");
if (!BASE_URL) {
    throw new Error("UL_PERF_BASE_URL is required. Run this through bin/run_perf_tests.sh.");
}

const MANIFEST = JSON.parse(open(__ENV.UL_PERF_MANIFEST || "/manifest.json"));

/** Where an unauthenticated request lands. Matched on, never requested. */
const LOGIN_PATH = "/accounts/login";

/** Requests per second the neighbour offers, whatever the server is doing. */
const RATE = Number(__ENV.UL_PERF_RATE || 5);

/** Measure the neighbour alone, with no actor and no thresholds. */
const BASELINE = (__ENV.UL_PERF_BASELINE || "") === "1";

/** How long the baseline pass runs. Long enough for a p95 to mean something. */
const BASELINE_SECONDS = Number(__ENV.UL_PERF_BASELINE_SECONDS || 60);

/** Where `handleSummary` writes the machine-readable result. */
const SUMMARY_PATH = __ENV.UL_PERF_SUMMARY || "";

/** The phases this run will actually execute. */
const ACTIVE = selectPhases(__ENV.UL_PERF_PHASES);

/** The p95 ceiling for the neighbour, in milliseconds. Supplied by the runner from a baseline pass, because a fixed number would be a statement about this host on the day it was written. */
const BUDGET_MS = Number(__ENV.UL_PERF_BUDGET_MS || 1000);

const FIXTURES = {
    labelId: Number(__ENV.UL_PERF_LABEL_ID || seedValue("label_id", 0)),
    labelName: __ENV.UL_PERF_LABEL_NAME || seedValue("label", "Perf Heavy"),
    labelUrlKind: __ENV.UL_PERF_LABEL_KIND || "tags",
    pinNamePrefix: __ENV.UL_PERF_PIN_PREFIX || "Perf Pin",
    importPins: Number(__ENV.UL_PERF_IMPORT_PINS || 500),
    // Longer than nginx's own timeout: the recorded outcome must be the server's 504, not the harness giving up first.
    importTimeout: __ENV.UL_PERF_IMPORT_TIMEOUT || "230s",
    actorTimeout: __ENV.UL_PERF_ACTOR_TIMEOUT || "120s",
    expectedPins: Number(__ENV.UL_PERF_EXPECTED_PINS || seedValue("pins", 0)),
};

/** The neighbour's rotation. One cheap, one representative, one that proves the
 * process is answering at all - so a failure says which layer went.
 */
const NEIGHBOUR_REQUESTS = [
    { endpoint: "health_ready", path: "/health/ready" },
    { endpoint: "map_pins", path: "/dashboard/map/pins/?limit=50" },
    { endpoint: "map_page", path: "/dashboard/map/" },
];

export const options = {
    scenarios: buildScenarios(),
    thresholds: buildThresholds(),
    // Timings stay honest without keeping bodies; otherwise the generator OOMs on multi-MB map documents.
    discardResponseBodies: true,
    summaryTrendStats: ["min", "med", "avg", "p(90)", "p(95)", "p(99)", "max", "count"],
    noConnectionReuse: false,
    // Generous: the pre-flight touches the slow thing before the run commits to it.
    setupTimeout: "180s",
};

/** Per-VU, because each VU is its own JS runtime with its own cookie jar. */
let vuSession = null;

/** Check the run is worth starting before spending fifteen minutes on it. Signs in as the heavy account and asks how many pins it has. */
export function setup() {
    const secondary = signIn(BASE_URL, accountFor("secondary"));
    if (BASELINE) {
        // The baseline pass never touches the heavy account, so requiring it
        // seeded would refuse to measure a machine perfectly capable of
        // producing the number this pass exists for.
        return { cookies: { secondary: secondary.cookies } };
    }

    const heavy = accountFor("heavy");
    const session = signIn(BASE_URL, heavy);
    // Two roles must end up with two sessions; a shared jar once made actor and neighbour the same account.
    if (session.cookies.sessionid === secondary.cookies.sessionid) {
        fail("The heavy and secondary sign-ins produced the same session. The second one did not happen, so the actor and the neighbour would be the same account.");
    }

    const response = get(session, "/dashboard/map/pins/?limit=1&include_total=1", { endpoint: "preflight", phase: "setup" }, { responseType: "text" });
    if (response.status !== 200) {
        fail(`Pre-flight GET map.pins answered ${response.status} as ${heavy.username}; the run would measure an error page.`);
    }

    let total = null;
    try {
        total = JSON.parse(response.body).total;
    } catch (error) {
        fail(`Pre-flight response was not JSON (${error}). Something in front of the app is rewriting it.`);
    }

    if (FIXTURES.expectedPins && total !== null && total < FIXTURES.expectedPins) {
        fail(
            `The heavy account holds ${total} pins, fewer than the ${FIXTURES.expectedPins} it was seeded to. ` +
                "Either provisioning did not run against this target, or this request was not made as the heavy account.",
        );
    }
    if (!FIXTURES.labelId) {
        fail("No heavy label id. Pass UL_PERF_LABEL_ID, or seed through provision_integration_env --heavy-pins so the manifest carries it.");
    }

    console.log(`pre-flight ok: ${total} pins on ${heavy.username}, label ${FIXTURES.labelId}, budget p95 < ${BUDGET_MS}ms`);
    return { total, cookies: { secondary: secondary.cookies, heavy: session.cookies } };
}

/** The user whose experience is the measurement. */
export function neighbour(data) {
    const session = sessionFor("secondary", data);
    const phase = currentPhase();
    const request = NEIGHBOUR_REQUESTS[exec.scenario.iterationInTest % NEIGHBOUR_REQUESTS.length];
    const response = get(session, request.path, { endpoint: request.endpoint, phase });
    check(response, { "neighbour got a 2xx": (r) => r.status >= 200 && r.status < 300 }, { phase });
    // Thresholded: a bounced session follows to the login page and records a fast 200 for the wrong page.
    check(response, { "still signed in": (r) => !r.url.includes(LOGIN_PATH) }, { guard: "signed_in" });
}

/** The user doing the expensive thing. */
export function actor(data) {
    const session = sessionFor("heavy", data);
    const phase = exec.scenario.name.replace(/^actor_/, "");
    const definition = ACTIVE.find((candidate) => candidate.name === phase);
    if (!definition || !definition.action) {
        fail(`Scenario ${exec.scenario.name} has no action in schedule.js.`);
    }
    const response = ACTIONS[definition.action](session, FIXTURES, { phase, actor: "1" });
    // Recorded, not asserted: only the neighbour's latency is a verdict. Session still thresholded (a bounced actor does no work).
    check(response, { "actor request completed": (r) => r.status !== 0 }, { phase, actor: "1" });
    check(response, { "still signed in": (r) => !r.url.includes(LOGIN_PATH) }, { guard: "signed_in" });
}

// -- wiring ----------------------------------------------------------------

function buildScenarios() {
    const scenarios = {
        neighbour: {
            executor: "constant-arrival-rate",
            rate: RATE,
            timeUnit: "1s",
            duration: `${BASELINE ? BASELINE_SECONDS : totalSeconds(ACTIVE)}s`,
            // Sized so that dropping an iteration means average latency crossed
            // 40 seconds, not that the generator was under-provisioned.
            preAllocatedVUs: Math.max(RATE * 4, 20),
            maxVUs: Math.max(RATE * 40, 200),
            exec: "neighbour",
        },
    };
    if (BASELINE) {
        return scenarios;
    }

    const starts = phaseStarts(ACTIVE);
    ACTIVE.forEach((phase, index) => {
        if (!phase.action) {
            return;
        }
        scenarios[`actor_${phase.name}`] = {
            executor: "constant-vus",
            vus: phase.vus,
            startTime: `${starts[index]}s`,
            duration: `${phase.seconds}s`,
            exec: "actor",
        };
    });
    return scenarios;
}

/** Thresholds, which in k6 are also the *only* way to ask for a sub-metric. A tag combination that no threshold names is never aggregated, so it is absent from the summary however many requests carried it. */
function buildThresholds() {
    const recordBaseline = { [`http_req_duration{scenario:neighbour,phase:${baselinePhase(ACTIVE)}}`]: ["p(95)>=0"] };
    if (BASELINE) {
        // Guard matters most here: a baseline against the sign-in page would judge every later phase against it.
        return Object.assign({ "checks{guard:signed_in}": ["rate==1"] }, recordBaseline);
    }
    const thresholds = {
        // Dropped iterations mean VUs stuck inside requests: the invariant failing, not a slow page.
        dropped_iterations: ["count==0"],
        // Not a performance assertion: proves the run measured the pages it claims.
        "checks{guard:signed_in}": ["rate==1"],
        "http_req_failed{scenario:neighbour}": ["rate==0"],
        // No ceiling on the reference itself: a threshold there would be circular.
        ...recordBaseline,
    };
    for (const phase of assertedPhases(ACTIVE)) {
        thresholds[`http_req_duration{scenario:neighbour,phase:${phase}}`] = [`p(95)<${BUDGET_MS}`];
    }
    return thresholds;
}

/** Write the result somewhere the runner can compute a verdict from. */
export function handleSummary(data) {
    const output = {};
    if (SUMMARY_PATH) {
        output[SUMMARY_PATH] = JSON.stringify(data, null, 2);
    }
    output.stdout = renderVerdict(data);
    return output;
}

function renderVerdict(data) {
    const lines = [];
    lines.push("");
    lines.push(BASELINE ? `baseline pass (${BASELINE_SECONDS}s, neighbour only)` : `measured pass (${totalSeconds(ACTIVE)}s, budget p95 < ${BUDGET_MS}ms)`);
    lines.push("");
    lines.push("  phase                 count      p50      p95      p99      max   verdict");

    const names = BASELINE ? [baselinePhase(ACTIVE)] : [baselinePhase(ACTIVE), ...assertedPhases(ACTIVE)];
    for (const phase of names) {
        const metric = data.metrics[`http_req_duration{scenario:neighbour,phase:${phase}}`];
        if (!metric) {
            lines.push(`  ${phase.padEnd(20)}  (no requests fell in this phase)`);
            continue;
        }
        const values = metric.values;
        const verdict = BASELINE || phase === baselinePhase() ? "-" : values["p(95)"] < BUDGET_MS ? "ok" : "OVER";
        lines.push(
            `  ${phase.padEnd(20)} ${String(values.count).padStart(6)} ${fixed(values.med)} ${fixed(values["p(95)"])} ${fixed(values["p(99)"])} ${fixed(values.max)}   ${verdict}`,
        );
    }

    const dropped = data.metrics.dropped_iterations;
    if (dropped && dropped.values.count > 0) {
        lines.push("");
        lines.push(`  ${dropped.values.count} iterations were DROPPED: every VU was inside a request when the next was due.`);
    }
    const failed = data.metrics["http_req_failed{scenario:neighbour}"];
    if (failed && failed.values.rate > 0) {
        lines.push(`  ${(failed.values.rate * 100).toFixed(2)}% of the neighbour's requests failed.`);
    }
    lines.push("");
    return lines.join("\n");
}

function fixed(value) {
    return (value === undefined ? "-" : value.toFixed(0)).padStart(8);
}

function currentPhase() {
    return phaseAt(exec.instance.currentTestRunDuration / 1000, ACTIVE);
}

/** This VU's session for `role`, adopted from `setup` rather than signed in. */
function sessionFor(role, data) {
    if (!vuSession) {
        const cookies = (data && data.cookies && data.cookies[role]) || null;
        if (!cookies) {
            fail(`setup() minted no session for "${role}". Every VU would sign itself in, which costs more CPU than the run measures.`);
        }
        vuSession = adopt(BASE_URL, role, cookies);
    }
    return vuSession;
}

function accountFor(role) {
    const found = (MANIFEST.accounts || []).find((account) => account.role === role);
    if (!found) {
        const roles = (MANIFEST.accounts || []).map((account) => account.role).join(", ") || "none";
        throw new Error(`The manifest has no "${role}" account. It has: ${roles}. Provision with --roles primary,secondary,heavy.`);
    }
    return found;
}

/** A value from the manifest's heavy-seed report, when provisioning wrote one. */
function seedValue(key, fallback) {
    const seeds = MANIFEST.seeds || {};
    const heavy = seeds.heavy || {};
    return heavy[key] === undefined ? fallback : heavy[key];
}
