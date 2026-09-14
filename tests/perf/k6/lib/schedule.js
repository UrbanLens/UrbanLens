/** The one place the run's timeline is written down. Two scenarios need to agree about it and they cannot talk to each other: k6 gives a VU no shared mutable state, so the neighbour cannot be told "the actor is importing now". */

/** Seconds at each end of a phase whose requests are tagged `edge` instead. The neighbour derives the phase from elapsed wall-clock, and the two clocks are offset by however long `setup()` took to log three accounts in - a couple of seconds. */
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
    // Last and longest: k6 never aborts in-flight requests, so an overrunning import only pollutes cooldown, not a measured phase.
    { name: "import_confirmed", seconds: 240, action: "importConfirmed", vus: 1 },
    // Not decoration. If the neighbour does not come back to baseline here,
    // something the actor did is still running, and a phase that merely ended
    // is not a phase that finished.
    { name: "cooldown", seconds: 120, action: null },
];

/**
 * A subset of the timeline, for running one phase without the other fourteen minutes. The baseline phase is always kept whether or not it was asked for: every verdict is relative to it, and a run without it has nothing to be relative to.
 *
 * @param {string|string[]|null} names Comma-separated or array; falsy means all.
 * @returns {object[]} The phases to run, in schedule order.
 */
export function selectPhases(names) {
    if (!names) {
        return PHASES;
    }
    const wanted = new Set((Array.isArray(names) ? names : String(names).split(",")).map((name) => name.trim()).filter(Boolean));
    if (!wanted.size) {
        return PHASES;
    }
    const unknown = [...wanted].filter((name) => !PHASES.some((phase) => phase.name === name));
    if (unknown.length) {
        throw new Error(`No such phase: ${unknown.join(", ")}. Known phases: ${PHASES.map((phase) => phase.name).join(", ")}.`);
    }
    return PHASES.filter((phase) => phase.baseline || wanted.has(phase.name));
}

/** Wall-clock second each phase begins at, relative to the start of the run. */
export function phaseStarts(phases = PHASES) {
    const starts = [];
    let elapsed = 0;
    for (const phase of phases) {
        starts.push(elapsed);
        elapsed += phase.seconds;
    }
    return starts;
}

/** Total run length in seconds. */
export function totalSeconds(phases = PHASES) {
    return phases.reduce((sum, phase) => sum + phase.seconds, 0);
}

/**
 * The phase tag for a request issued `elapsedSeconds` into the run.
 *
 * @param {number} elapsedSeconds Seconds since the run began.
 * @returns {string} A phase name, or `edge` within `GUARD_SECONDS` of a
 *     boundary, or `over` past the end of the schedule.
 */
export function phaseAt(elapsedSeconds, phases = PHASES) {
    const starts = phaseStarts(phases);
    for (let index = 0; index < phases.length; index += 1) {
        const start = starts[index];
        const end = start + phases[index].seconds;
        if (elapsedSeconds < start || elapsedSeconds >= end) {
            continue;
        }
        const nearStart = elapsedSeconds - start < GUARD_SECONDS;
        const nearEnd = end - elapsedSeconds < GUARD_SECONDS;
        return nearStart || nearEnd ? "edge" : phases[index].name;
    }
    return "over";
}

/** The phase the thresholds treat as the reference, by name. */
export function baselinePhase(phases = PHASES) {
    const found = phases.find((phase) => phase.baseline);
    return found ? found.name : phases[0].name;
}

/** Phase names a threshold should be asserted on: every acting phase, plus cooldown. */
export function assertedPhases(phases = PHASES) {
    return phases.filter((phase) => !phase.baseline).map((phase) => phase.name);
}
