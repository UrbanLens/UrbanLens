/**
 * Numbers a run produces - timings and counts - written as JSON lines so a run can be compared with
 * the last one and with `metrics-baseline.json`. `lib/metrics-reporter.ts` folds them at the end of
 * the run; `scripts/metrics-report.ts` compares. See docs/INTEGRATION_TESTS.md, "Metrics".
 */

import { appendFileSync, mkdirSync, readFileSync } from "node:fs";
import { relative, resolve } from "node:path";

import { test, type Page } from "@playwright/test";

import { INTEGRATION_ROOT } from "./env.js";
import { currentRun, REPORTS_DIR } from "./run.js";

export const METRICS_DIR = resolve(REPORTS_DIR, "metrics");
export const LATEST_PATH = resolve(METRICS_DIR, "latest.json");
export const PREVIOUS_PATH = resolve(METRICS_DIR, "previous.json");
export const TREND_PATH = resolve(METRICS_DIR, "trend.jsonl");

export type MetricUnit = "ms" | "s" | "count" | "bytes" | "sqm" | "ratio";

export type MetricTags = Record<string, string | number | boolean>;

export interface Metric {
    /** Dotted, lower snake case, unit last where it is one: `hrsh.pin_page.dom_content_loaded_ms`. */
    name: string;
    value: number;
    unit: MetricUnit;
    /** Spec file; defaults to the calling test's. */
    spec?: string;
    tags?: MetricTags;
}

/** One line of `reports/metrics/<runId>.jsonl`. */
export interface MetricRecord extends Metric {
    runId: string;
    startedAt: string;
    at: string;
    test?: string;
    project?: string;
}

/** Every sample of one metric in a run, reduced. `value` is the median, which is what baselines compare by default. */
export interface MetricSummary {
    unit: MetricUnit;
    count: number;
    value: number;
    min: number;
    max: number;
    mean: number;
    last: number;
}

export interface OutcomeCounts {
    passed: number;
    failed: number;
    flaky: number;
    skipped: number;
}

/** `reports/metrics/latest.json`, and one line of `trend.jsonl` minus `outcomes`. */
export interface RunSummary {
    runId: string;
    startedAt: string;
    finishedAt: string;
    gitSha: string | null;
    baseUrl: string;
    status: string;
    durationMs: number;
    tests: OutcomeCounts & { byProject: Record<string, OutcomeCounts> };
    metrics: Record<string, MetricSummary>;
    /** `project › file › title` to Playwright outcome, for spotting newly failing tests. latest.json only. */
    outcomes?: Record<string, string>;
}

const NAME_PATTERN = /^[a-z0-9_]+(\.[a-z0-9_]+)+$/;

/** Path of the JSON-lines file for a run id. */
export function metricsFileFor(runId: string): string {
    return resolve(METRICS_DIR, `${runId.replace(/[^A-Za-z0-9._-]/g, "_")}.jsonl`);
}

/** The calling test, when there is one; worker fixtures have none. */
function currentTest(): { spec: string; title: string; project: string; annotate: (text: string) => void } | null {
    try {
        const info = test.info();
        return {
            spec: relative(INTEGRATION_ROOT, info.file),
            title: info.titlePath.slice(1).join(" › "),
            project: info.project.name,
            annotate: (text) => info.annotations.push({ type: "metric", description: text }),
        };
    } catch {
        return null;
    }
}

/** Appends one sample to this run's metrics file, and annotates the calling test with it. */
export function recordMetric(metric: Metric): void {
    if (!NAME_PATTERN.test(metric.name)) {
        throw new Error(`Metric name "${metric.name}" must be dotted lower snake case, e.g. hrsh.child_pins.count.`);
    }
    if (!Number.isFinite(metric.value)) {
        throw new Error(`Metric ${metric.name} got a non-finite value (${metric.value}); record a number or nothing.`);
    }
    const run = currentRun();
    const caller = currentTest();
    const record: MetricRecord = {
        ...metric,
        spec: metric.spec ?? caller?.spec,
        runId: run.runId,
        startedAt: run.startedAt,
        at: new Date().toISOString(),
        test: caller?.title,
        project: caller?.project,
    };
    caller?.annotate(`${metric.name} = ${metric.value} ${metric.unit}`);
    try {
        mkdirSync(METRICS_DIR, { recursive: true });
        appendFileSync(metricsFileFor(run.runId), `${JSON.stringify(record)}\n`, "utf8");
    } catch (error) {
        // A lost sample shows as "not measured" in the report; failing the test for it would be worse.
        process.stderr.write(`metrics: could not record ${metric.name}: ${(error as Error).message}\n`);
    }
}

/** Runs `fn` and records its duration in milliseconds under `name` - on success only, so a timeout is not a sample. */
export async function timed<T>(name: string, fn: () => Promise<T>, extra: { spec?: string; tags?: MetricTags } = {}): Promise<T> {
    const started = performance.now();
    const result = await fn();
    recordMetric({ name, value: Math.round(performance.now() - started), unit: "ms", ...extra });
    return result;
}

/** Navigation Timing for the page's current document, in milliseconds from navigation start. Null where the event has not happened. */
export interface PageTimings {
    ttfbMs: number | null;
    domContentLoadedMs: number | null;
    loadMs: number | null;
    transferBytes: number | null;
}

/** Navigation Timing for the page's current document, without recording it. `waitForLoadMs` bounds a wait for the load event first. */
export async function readPageTimings(page: Page, waitForLoadMs = 15_000): Promise<PageTimings> {
    if (waitForLoadMs > 0) {
        await page.waitForLoadState("load", { timeout: waitForLoadMs }).catch(() => undefined);
    }
    return page.evaluate((): PageTimings => {
        const entry = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming | undefined;
        if (!entry) {
            return { ttfbMs: null, domContentLoadedMs: null, loadMs: null, transferBytes: null };
        }
        const positive = (value: number): number | null => (value > 0 ? Math.round(value) : null);
        return {
            ttfbMs: positive(entry.responseStart - entry.startTime),
            domContentLoadedMs: positive(entry.domContentLoadedEventEnd - entry.startTime),
            loadMs: positive(entry.loadEventEnd - entry.startTime),
            transferBytes: entry.transferSize > 0 ? entry.transferSize : null,
        };
    });
}

/** Records `<prefix>.ttfb_ms`, `.dom_content_loaded_ms` and `.load_ms` for timings already read. */
export function recordPageTimings(timings: PageTimings, prefix: string, extra: { spec?: string; tags?: MetricTags } = {}): void {
    const samples: Array<[string, number | null]> = [
        ["ttfb_ms", timings.ttfbMs],
        ["dom_content_loaded_ms", timings.domContentLoadedMs],
        ["load_ms", timings.loadMs],
    ];
    for (const [suffix, value] of samples) {
        if (value !== null) {
            recordMetric({ name: `${prefix}.${suffix}`, value, unit: "ms", ...extra });
        }
    }
}

/** Reads Navigation Timing and records it under `prefix`; see {@link readPageTimings} and {@link recordPageTimings}. */
export async function pageTimings(page: Page, prefix: string, options: { waitForLoadMs?: number; spec?: string; tags?: MetricTags } = {}): Promise<PageTimings> {
    const timings = await readPageTimings(page, options.waitForLoadMs);
    recordPageTimings(timings, prefix, { spec: options.spec, tags: options.tags });
    return timings;
}

/** Every sample recorded by the invocation that started at `startedAt`. */
export function readRunMetrics(runId: string, startedAt: string): MetricRecord[] {
    let text: string;
    try {
        text = readFileSync(metricsFileFor(runId), "utf8");
    } catch {
        return [];
    }
    const records: MetricRecord[] = [];
    for (const line of text.split("\n")) {
        if (!line.trim()) {
            continue;
        }
        try {
            const record = JSON.parse(line) as MetricRecord;
            if (record.startedAt === startedAt) {
                records.push(record);
            }
        } catch {
            // A torn line from a killed worker; the rest of the file is still good.
        }
    }
    return records;
}

/** Reduces samples to one summary per metric name. */
export function foldMetrics(records: readonly MetricRecord[]): Record<string, MetricSummary> {
    const byName = new Map<string, MetricRecord[]>();
    for (const record of records) {
        const list = byName.get(record.name) ?? [];
        list.push(record);
        byName.set(record.name, list);
    }
    const summaries: Record<string, MetricSummary> = {};
    for (const [name, list] of [...byName].sort(([a], [b]) => a.localeCompare(b))) {
        const values = list.map((record) => record.value);
        const sorted = [...values].sort((a, b) => a - b);
        const middle = Math.floor(sorted.length / 2);
        const median = sorted.length % 2 ? sorted[middle]! : (sorted[middle - 1]! + sorted[middle]!) / 2;
        summaries[name] = {
            unit: list[list.length - 1]!.unit,
            count: values.length,
            value: median,
            min: sorted[0]!,
            max: sorted[sorted.length - 1]!,
            mean: values.reduce((total, value) => total + value, 0) / values.length,
            last: values[values.length - 1]!,
        };
    }
    return summaries;
}
