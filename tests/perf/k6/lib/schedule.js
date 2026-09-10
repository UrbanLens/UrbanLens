/**
 * The one place the run's timeline is written down.
 *
 * Two scenarios need to agree about it and they cannot talk to each other: k6
 * gives a VU no shared mutable state, so the neighbour cannot be told "the
 * actor is importing now". Both derive it from the clock instead, from this
 * table, which is why the table rather than the scenario definitions is the
 * source of truth - the actor's `startTime` offsets and the neighbour's phase
 * lookup are both computed from it below.
 *
 * A phase's `action` names an export of `actions.js`. `vus` is how many copies
 * of the actor run it at once; that is the whole difference between
 * `map_search_1` and `map_search_8`, and between either and `search_storm`.
 */

/** Seconds at each end of a phase whose requests are tagged `edge` instead.
 *
 * The neighbour derives the phase from elapsed wall-clock, and the two clocks
 * are offset by however long `setup()` took to log three accounts in - a couple
 * of seconds. Without a guard band a request from the tail of `search_storm`
 * can be tagged `cooldown` and fail cooldown's threshold, which reads as a
 * regression in the quietest phase of the run.
 */
export const GUARD_SECONDS = 5;

export const PHASES = [
    {
        name: "idle",
        seconds: 60,
        action: null,
        // The baseline every other phase is compared against, measured in the
        // same process on the same host in the same run. A baseline from
        // another run would fold this host's current load into the verdict.
        baseline: true,
    },
    { name: "map_init_1", seconds: 60, action: "mapInit", vus: 1 },
    { name: "map_init_4", seconds: 60, action: "mapInit", vus: 4 },
    { name: "map_search_1", seconds: 60, action: "mapSearch", vus: 1 },
    { name: "map_search_8", seconds: 90, action: "mapSearch", vus: 8 },
    { name: "label_edit", seconds: 90, action: "labelEdit", vus: 1 },
    // P104's mechanism, reproduced deliberately: enough concurrent filter POSTs
    // to exhaust the connection pool if nothing bounds it.
    { name: "search_storm", seconds: 90, action: "mapSearch", vus: 60 },
    // Last of the acting phases, and long, because it is the only one whose
    // duration is not ours to choose: k6 does not abort an in-flight request at
    // a phase boundary, so an import that outlives its phase goes on loading
    // the box while the next phase is being measured. Ordered here so the phase
    // that inherits an overrun is cooldown, where a raised p95 is a true signal
    // (the import is still running) rather than a false one.
    { name: "import_confirmed", seconds: 240, action: "importConfirmed", vus: 1 },
    // Not decoration. If the neighbour does not come back to baseline here,
    // something the actor did is still running, and a phase that merely ended
    // is not a phase that finished.
    { name: "cooldown", seconds: 120, action: null },
];

/** Wall-clock second each phase begins at, relative to the start of the run. */
export function phaseStarts() {
    const starts = [];
    let elapsed = 0;
    for (const phase of PHASES) {
        starts.push(elapsed);
        elapsed += phase.seconds;
    }
    return starts;
}

/** Total run length in seconds. */
export function totalSeconds() {
    return PHASES.reduce((sum, phase) => sum + phase.seconds, 0);
}

/**
 * The phase tag for a request issued `elapsedSeconds` into the run.
 *
 * @param {number} elapsedSeconds Seconds since the run began.
 * @returns {string} A phase name, or `edge` within `GUARD_SECONDS` of a
 *     boundary, or `over` past the end of the schedule.
 */
export function phaseAt(elapsedSeconds) {
    const starts = phaseStarts();
    for (let index = 0; index < PHASES.length; index += 1) {
        const start = starts[index];
        const end = start + PHASES[index].seconds;
        if (elapsedSeconds < start || elapsedSeconds >= end) {
            continue;
        }
        const nearStart = elapsedSeconds - start < GUARD_SECONDS;
        const nearEnd = end - elapsedSeconds < GUARD_SECONDS;
        return nearStart || nearEnd ? "edge" : PHASES[index].name;
    }
    return "over";
}

/** The phase the thresholds treat as the reference, by name. */
export function baselinePhase() {
    const found = PHASES.find((phase) => phase.baseline);
    return found ? found.name : PHASES[0].name;
}

/** Phase names a threshold should be asserted on: every acting phase, plus cooldown. */
export function assertedPhases() {
    return PHASES.filter((phase) => !phase.baseline).map((phase) => phase.name);
}
