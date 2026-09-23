/**
 * Folds the run's metric samples into `reports/metrics/latest.json` and appends a summary row to
 * `trend.jsonl`. A reporter rather than a global teardown because `onEnd` runs after teardown and
 * is the only hook that knows the run's outcomes.
 */

import { appendFileSync, existsSync, mkdirSync, renameSync, writeFileSync } from "node:fs";
import { relative } from "node:path";

import type { FullResult, Reporter, Suite, TestCase } from "@playwright/test/reporter";

import { INTEGRATION_ROOT } from "./env.js";
import { foldMetrics, LATEST_PATH, METRICS_DIR, PREVIOUS_PATH, readRunMetrics, TREND_PATH, type OutcomeCounts, type RunSummary } from "./metrics.js";
import { currentRun } from "./run.js";

function emptyCounts(): OutcomeCounts {
    return { passed: 0, failed: 0, flaky: 0, skipped: 0 };
}

function countInto(counts: OutcomeCounts, outcome: ReturnType<TestCase["outcome"]>): void {
    if (outcome === "expected") {
        counts.passed += 1;
    } else if (outcome === "unexpected") {
        counts.failed += 1;
    } else if (outcome === "flaky") {
        counts.flaky += 1;
    } else {
        counts.skipped += 1;
    }
}

export default class MetricsReporter implements Reporter {
    private suite: Suite | null = null;

    printsToStdio(): boolean {
        return false;
    }

    onBegin(_config: unknown, suite: Suite): void {
        this.suite = suite;
    }

    onEnd(result: FullResult): void {
        const run = currentRun();
        const tests = (this.suite?.allTests() ?? []).filter((test) => test.results.length > 0);
        // A process that ran nothing (`--list`) would otherwise fold in whichever run owns reports/run.json.
        if (tests.length === 0) {
            return;
        }
        const records = readRunMetrics(run.runId, run.startedAt);

        const totals = emptyCounts();
        const byProject: Record<string, OutcomeCounts> = {};
        const outcomes: Record<string, string> = {};
        for (const test of tests) {
            const project = test.parent.project()?.name ?? "";
            const outcome = test.outcome();
            countInto(totals, outcome);
            countInto((byProject[project] ??= emptyCounts()), outcome);
            outcomes[`${project} › ${relative(INTEGRATION_ROOT, test.location.file)} › ${test.titlePath().slice(3).join(" › ")}`] = outcome;
        }

        const summary: RunSummary = {
            runId: run.runId,
            startedAt: run.startedAt,
            finishedAt: new Date().toISOString(),
            gitSha: run.gitSha,
            baseUrl: run.baseUrl,
            status: result.status,
            durationMs: Math.round(result.duration),
            tests: { ...totals, byProject },
            metrics: foldMetrics(records),
        };

        try {
            mkdirSync(METRICS_DIR, { recursive: true });
            if (existsSync(LATEST_PATH)) {
                renameSync(LATEST_PATH, PREVIOUS_PATH);
            }
            writeFileSync(LATEST_PATH, JSON.stringify({ ...summary, outcomes }, null, 2), "utf8");
            appendFileSync(TREND_PATH, `${JSON.stringify(summary)}\n`, "utf8");
            process.stdout.write(`  metrics     ${records.length} sample(s) of ${Object.keys(summary.metrics).length} metric(s) -> ${relative(process.cwd(), LATEST_PATH)}\n`);
        } catch (error) {
            process.stderr.write(`metrics: could not write the run summary: ${(error as Error).message}\n`);
        }
    }
}
