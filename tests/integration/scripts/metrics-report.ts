/**
 * Compares the latest run's metrics with the previous run and with `metrics-baseline.json`.
 *
 *   npm run metrics:report                       exit 1 on a breach, 2 when no run has been folded yet
 *   npm run metrics:report -- --strict           a baselined metric the run did not measure is a breach too
 *   npm run metrics:report -- --ratchet          tighten the baseline to this run, never loosen it; review the diff
 *   npm run metrics:report -- --baseline <path>
 *
 * Self-contained (node builtins only) so it runs under `node --experimental-strip-types` without the suite's imports.
 */

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const METRICS_DIR = resolve(ROOT, "reports", "metrics");
const LATEST_PATH = resolve(METRICS_DIR, "latest.json");
const PREVIOUS_PATH = resolve(METRICS_DIR, "previous.json");
const TREND_PATH = resolve(METRICS_DIR, "trend.jsonl");
const DEFAULT_BASELINE_PATH = resolve(ROOT, "metrics-baseline.json");

/** Mirrors `MetricSummary` in lib/metrics.ts. */
interface Summary {
    unit: string;
    count: number;
    value: number;
    min: number;
    max: number;
    mean: number;
    last: number;
}

type Stat = "value" | "min" | "max" | "mean" | "last" | "count";

interface Counts {
    passed: number;
    failed: number;
    flaky: number;
    skipped: number;
}

/** Mirrors `RunSummary` in lib/metrics.ts. */
interface RunSummary {
    runId: string;
    startedAt: string;
    finishedAt: string;
    gitSha: string | null;
    baseUrl: string;
    status: string;
    durationMs: number;
    tests: Counts;
    metrics: Record<string, Summary>;
    outcomes?: Record<string, string>;
}

interface Bound {
    min?: number;
    max?: number;
    /** Which summary field is compared; the median by default. */
    stat?: Stat;
    /** Fraction of slack `--ratchet` leaves above a `max` (default 0.5). `min` is ratcheted to the value itself. */
    headroom?: number;
    /** False keeps `--ratchet` off this entry. */
    ratchet?: boolean;
    note?: string;
}

interface Baseline {
    metrics: Record<string, Bound>;
    [key: string]: unknown;
}

interface Row {
    name: string;
    unit: string;
    latest: number | null;
    previous: number | null;
    bound: Bound | null;
    status: string;
    breach: boolean;
}

function parseArgs(argv: readonly string[]): { baselinePath: string; strict: boolean; ratchet: boolean } {
    let baselinePath = DEFAULT_BASELINE_PATH;
    let strict = false;
    let ratchet = false;
    for (let index = 0; index < argv.length; index += 1) {
        const arg = argv[index];
        if (arg === "--strict") {
            strict = true;
        } else if (arg === "--ratchet") {
            ratchet = true;
        } else if (arg === "--baseline") {
            const next = argv[index + 1];
            if (!next) {
                fail("--baseline needs a path.");
            }
            baselinePath = resolve(process.cwd(), next);
            index += 1;
        } else {
            fail(`Unknown argument ${arg}. Accepted: --strict, --ratchet, --baseline <path>.`);
        }
    }
    return { baselinePath, strict, ratchet };
}

function fail(message: string, code = 2): never {
    process.stderr.write(`metrics-report: ${message}\n`);
    process.exit(code);
}

function readJson<T>(path: string): T | null {
    if (!existsSync(path)) {
        return null;
    }
    try {
        return JSON.parse(readFileSync(path, "utf8")) as T;
    } catch (error) {
        fail(`${relative(process.cwd(), path)} is not valid JSON: ${(error as Error).message}`);
    }
}

function readTrend(): RunSummary[] {
    if (!existsSync(TREND_PATH)) {
        return [];
    }
    const rows: RunSummary[] = [];
    for (const line of readFileSync(TREND_PATH, "utf8").split("\n")) {
        if (!line.trim()) {
            continue;
        }
        try {
            rows.push(JSON.parse(line) as RunSummary);
        } catch {
            // A torn row from an interrupted write; the rest of the history is still usable.
        }
    }
    return rows;
}

function statOf(summary: Summary, stat: Stat = "value"): number {
    return summary[stat];
}

/** The most recent earlier run that measured `name`. */
function previousValue(history: readonly RunSummary[], name: string, stat: Stat | undefined): number | null {
    for (let index = history.length - 1; index >= 0; index -= 1) {
        const summary = history[index]?.metrics[name];
        if (summary) {
            return statOf(summary, stat);
        }
    }
    return null;
}

function judge(value: number | null, bound: Bound | null, strict: boolean): { status: string; breach: boolean } {
    if (bound === null) {
        return { status: "no baseline", breach: false };
    }
    if (value === null) {
        return { status: strict ? "BREACH (not measured)" : "not measured", breach: strict };
    }
    if (bound.min !== undefined && value < bound.min) {
        return { status: `BREACH (< ${bound.min})`, breach: true };
    }
    if (bound.max !== undefined && value > bound.max) {
        return { status: `BREACH (> ${bound.max})`, breach: true };
    }
    return { status: "ok", breach: false };
}

function formatNumber(value: number | null): string {
    if (value === null) {
        return "-";
    }
    return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

function formatDelta(latest: number | null, previous: number | null): string {
    if (latest === null || previous === null || previous === 0) {
        return "-";
    }
    const delta = ((latest - previous) / Math.abs(previous)) * 100;
    return `${delta >= 0 ? "+" : ""}${delta.toFixed(1)}%`;
}

function formatBound(bound: Bound | null): string {
    if (bound === null) {
        return "-";
    }
    const parts: string[] = [];
    if (bound.min !== undefined) {
        parts.push(`>= ${bound.min}`);
    }
    if (bound.max !== undefined) {
        parts.push(`<= ${bound.max}`);
    }
    return `${parts.join(", ") || "-"}${bound.stat && bound.stat !== "value" ? ` (${bound.stat})` : ""}`;
}

function printTable(rows: readonly string[][]): void {
    const widths = rows[0]?.map((_, column) => Math.max(...rows.map((row) => (row[column] ?? "").length))) ?? [];
    for (const row of rows) {
        process.stdout.write(`  ${row.map((cell, column) => cell.padEnd(widths[column] ?? 0)).join("  ").trimEnd()}\n`);
    }
}

/** Tightens each bound towards this run's value. Returns one line per change. */
function ratchetBaseline(baseline: Baseline, latest: RunSummary): string[] {
    const changes: string[] = [];
    for (const [name, bound] of Object.entries(baseline.metrics)) {
        const summary = latest.metrics[name];
        if (!summary || bound.ratchet === false) {
            continue;
        }
        const value = statOf(summary, bound.stat);
        if (bound.max !== undefined) {
            const tightened = Math.ceil(value * (1 + (bound.headroom ?? 0.5)));
            if (tightened < bound.max) {
                changes.push(`${name}: max ${bound.max} -> ${tightened}`);
                bound.max = tightened;
            }
        }
        if (bound.min !== undefined && value > bound.min) {
            changes.push(`${name}: min ${bound.min} -> ${value}`);
            bound.min = value;
        }
    }
    return changes;
}

/** One metric per line, as the committed file is written, so a ratchet diff shows only the bounds that moved. */
function formatBaseline(baseline: Baseline): string {
    const { metrics, ...rest } = baseline;
    const inline = (bound: Bound) => `{ ${Object.entries(bound).map(([key, value]) => `${JSON.stringify(key)}: ${JSON.stringify(value)}`).join(", ")} }`;
    const lines = [
        ...Object.entries(rest).map(([key, value]) => `    ${JSON.stringify(key)}: ${JSON.stringify(value)},`),
        '    "metrics": {',
        Object.entries(metrics).map(([name, bound]) => `        ${JSON.stringify(name)}: ${inline(bound)}`).join(",\n"),
        "    }",
    ];
    return `{\n${lines.join("\n")}\n}\n`;
}

function main(): void {
    const { baselinePath, strict, ratchet } = parseArgs(process.argv.slice(2));

    const latest = readJson<RunSummary>(LATEST_PATH);
    if (latest === null) {
        fail(`No ${relative(process.cwd(), LATEST_PATH)} yet. Run the suite first; lib/metrics-reporter.ts writes it when the run ends.`);
    }
    const baseline = readJson<Baseline>(baselinePath) ?? { metrics: {} };
    baseline.metrics ??= {};
    const previousRun = readJson<RunSummary>(PREVIOUS_PATH);
    const history = readTrend().filter((row) => !(row.runId === latest.runId && row.startedAt === latest.startedAt));

    const names = [...new Set([...Object.keys(latest.metrics), ...Object.keys(baseline.metrics)])].sort();
    const rows: Row[] = names.map((name) => {
        const bound = baseline.metrics[name] ?? null;
        const summary = latest.metrics[name];
        const value = summary ? statOf(summary, bound?.stat) : null;
        return {
            name,
            unit: summary?.unit ?? "",
            latest: value,
            previous: previousValue(history, name, bound?.stat),
            bound,
            ...judge(value, bound, strict),
        };
    });

    process.stdout.write(
        `\nRun ${latest.runId} (${latest.startedAt}), commit ${latest.gitSha ?? "unknown"}, against ${latest.baseUrl}: ${latest.status}, ` +
            `${latest.tests.passed} passed, ${latest.tests.failed} failed, ${latest.tests.flaky} flaky, ${latest.tests.skipped} skipped.\n` +
            `Baseline: ${relative(process.cwd(), baselinePath)}${existsSync(baselinePath) ? "" : " (missing)"}\n\n`,
    );
    if (rows.length === 0) {
        process.stdout.write("  No metrics were recorded and none are baselined.\n");
    } else {
        printTable([
            ["metric", "unit", "latest", "previous", "change", "baseline", "status"],
            ...rows.map((row) => [row.name, row.unit, formatNumber(row.latest), formatNumber(row.previous), formatDelta(row.latest, row.previous), formatBound(row.bound), row.status]),
        ]);
    }

    if (previousRun?.outcomes && latest.outcomes) {
        const changed = Object.entries(latest.outcomes)
            .filter(([test, outcome]) => previousRun.outcomes?.[test] !== undefined && previousRun.outcomes[test] !== outcome)
            .map(([test, outcome]) => `  ${previousRun.outcomes?.[test]} -> ${outcome}  ${test}`);
        if (changed.length > 0) {
            process.stdout.write(`\nTests whose outcome changed since the previous run (${changed.length}):\n${changed.slice(0, 50).join("\n")}\n`);
            if (changed.length > 50) {
                process.stdout.write(`  ... and ${changed.length - 50} more; see ${relative(process.cwd(), LATEST_PATH)}.\n`);
            }
        }
    }

    if (ratchet) {
        const changes = ratchetBaseline(baseline, latest);
        if (changes.length > 0) {
            writeFileSync(baselinePath, formatBaseline(baseline), "utf8");
        }
        process.stdout.write(`\nRatchet: ${changes.length ? `${changes.join("; ")}. Review and commit ${relative(process.cwd(), baselinePath)}.` : "nothing to tighten."}\n`);
    }

    const breaches = rows.filter((row) => row.breach);
    if (breaches.length > 0) {
        process.stdout.write(`\n${breaches.length} metric(s) breach the baseline: ${breaches.map((row) => row.name).join(", ")}.\n`);
        process.exit(1);
    }
    process.stdout.write("\nNo baseline breaches.\n");
}

main();
