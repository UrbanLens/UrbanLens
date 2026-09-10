/**
 * `schedule.js` decides two things that nothing else can check.
 *
 * It is the only agreement between the actor's scenarios and the neighbour's
 * phase tags - they cannot talk to each other at run time, so if the timeline
 * and the lookup ever disagree the run still completes and reports numbers, but
 * they are attributed to the wrong phase. That failure is silent by
 * construction, which is why it is worth a test that costs nothing.
 *
 * Runs under `bun test`, because this module is deliberately free of k6 imports.
 */

import { describe, expect, test } from "bun:test";

import { GUARD_SECONDS, PHASES, assertedPhases, baselinePhase, phaseAt, phaseStarts, selectPhases, totalSeconds } from "./schedule.js";

describe("the timeline", () => {
    test("starts are the running total of the durations", () => {
        const starts = phaseStarts();
        expect(starts).toHaveLength(PHASES.length);
        expect(starts[0]).toBe(0);
        for (let index = 1; index < PHASES.length; index += 1) {
            expect(starts[index]).toBe(starts[index - 1] + PHASES[index - 1].seconds);
        }
    });

    test("the total is the sum of the phases", () => {
        expect(totalSeconds()).toBe(PHASES.reduce((sum, phase) => sum + phase.seconds, 0));
    });

    test("stays inside a quarter of an hour", () => {
        // Not an arbitrary limit: the runner is meant to be runnable while
        // waiting for it, and a suite people will not wait for does not get run.
        expect(totalSeconds()).toBeLessThanOrEqual(15 * 60);
    });
});

describe("phase lookup", () => {
    test("names each phase at its midpoint", () => {
        const starts = phaseStarts();
        PHASES.forEach((phase, index) => {
            expect(phaseAt(starts[index] + phase.seconds / 2)).toBe(phase.name);
        });
    });

    test("guards both edges of every phase", () => {
        const starts = phaseStarts();
        PHASES.forEach((phase, index) => {
            expect(phaseAt(starts[index] + 1)).toBe("edge");
            expect(phaseAt(starts[index] + phase.seconds - 1)).toBe("edge");
        });
    });

    test("every phase is long enough to survive its own guard bands", () => {
        // A phase shorter than two guard bands would be tagged `edge` end to
        // end and silently contribute nothing to the verdict.
        for (const phase of PHASES) {
            expect(phase.seconds).toBeGreaterThan(GUARD_SECONDS * 2);
        }
    });

    test("says `over` past the end rather than guessing", () => {
        expect(phaseAt(totalSeconds() + 1)).toBe("over");
    });

    test("covers the whole run with no gap between phases", () => {
        const named = new Set();
        for (let second = 0; second < totalSeconds(); second += 1) {
            const phase = phaseAt(second + 0.5);
            expect(phase).not.toBe("over");
            named.add(phase);
        }
        for (const phase of PHASES) {
            expect(named.has(phase.name)).toBe(true);
        }
    });
});

describe("what gets asserted", () => {
    test("the baseline phase is recorded but never judged", () => {
        expect(PHASES.map((phase) => phase.name)).toContain(baselinePhase());
        expect(assertedPhases()).not.toContain(baselinePhase());
    });

    test("every other phase is judged", () => {
        expect(assertedPhases()).toHaveLength(PHASES.length - 1);
    });

    test("exactly one phase is the baseline", () => {
        expect(PHASES.filter((phase) => phase.baseline)).toHaveLength(1);
    });
});

describe("the actor's half", () => {
    test("every acting phase says how many copies run it", () => {
        for (const phase of PHASES.filter((candidate) => candidate.action)) {
            expect(phase.vus).toBeGreaterThan(0);
        }
    });

    test("every named action is actually exported by actions.js", async () => {
        // Read rather than imported: `actions.js` imports `k6/http`, which does
        // not resolve outside k6. A schedule naming an action that does not
        // exist fails at run time, mid-phase, having already spent the minutes
        // before it.
        const source = await Bun.file(new URL("./actions.js", import.meta.url)).text();
        for (const phase of PHASES.filter((candidate) => candidate.action)) {
            expect(source).toContain(`export function ${phase.action}(`);
        }
    });

    test("phase names are unique", () => {
        // They become k6 scenario keys and threshold tags; a duplicate would
        // silently merge two phases' measurements.
        const names = PHASES.map((phase) => phase.name);
        expect(new Set(names).size).toBe(names.length);
    });

    test("no phase is named after the lookup's own sentinels", () => {
        for (const phase of PHASES) {
            expect(["edge", "over"]).not.toContain(phase.name);
        }
    });
});

describe("running a subset", () => {
    test("no selection means the whole schedule", () => {
        expect(selectPhases(null)).toBe(PHASES);
        expect(selectPhases("")).toBe(PHASES);
        expect(selectPhases([])).toBe(PHASES);
    });

    test("keeps the baseline whether or not it was asked for", () => {
        // Every verdict is relative to it, so a subset without it has nothing
        // to be relative to.
        const chosen = selectPhases("import_confirmed");
        expect(chosen.map((phase) => phase.name)).toContain(baselinePhase());
        expect(chosen.map((phase) => phase.name)).toContain("import_confirmed");
    });

    test("stays in schedule order, not argument order", () => {
        const chosen = selectPhases("search_storm,map_init_1");
        const order = PHASES.map((phase) => phase.name);
        const positions = chosen.map((phase) => order.indexOf(phase.name));
        expect(positions).toEqual([...positions].sort((a, b) => a - b));
    });

    test("refuses a phase that does not exist", () => {
        // Silently running fewer phases than asked for is how a run reports a
        // clean table for work it never did.
        expect(() => selectPhases("import_confirmd")).toThrow(/No such phase/);
    });

    test("the derived helpers follow the subset", () => {
        const chosen = selectPhases("import_confirmed");

        expect(totalSeconds(chosen)).toBeLessThan(totalSeconds());
        expect(assertedPhases(chosen)).toEqual(["import_confirmed"]);
        const starts = phaseStarts(chosen);
        expect(starts).toHaveLength(chosen.length);
        expect(phaseAt(starts[1] + chosen[1].seconds / 2, chosen)).toBe("import_confirmed");
    });

    test("a subset still covers its own run with no gap", () => {
        const chosen = selectPhases("label_edit,cooldown");
        for (let second = 0; second < totalSeconds(chosen); second += 1) {
            expect(phaseAt(second + 0.5, chosen)).not.toBe("over");
        }
    });
});
