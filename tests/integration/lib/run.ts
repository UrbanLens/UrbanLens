/**
 * One identity per `playwright test` invocation. Workers are separate processes that each compute
 * their own `env.runId`, so global setup writes the main process's identity to `reports/run.json`
 * and everything keyed by run reads it back from there.
 */

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { env, INTEGRATION_ROOT } from "./env.js";

export const REPORTS_DIR = resolve(INTEGRATION_ROOT, "reports");
export const RUN_INFO_PATH = resolve(REPORTS_DIR, "run.json");

/** What global setup records about this invocation. */
export interface RunInfo {
    runId: string;
    /** ISO timestamp; with `runId`, distinguishes invocations that share a fixed `UL_E2E_RUN_ID`. */
    startedAt: string;
    gitSha: string | null;
    baseUrl: string;
}

/** Short commit hash of the checkout, or null when git is unavailable (the Docker runner mounts only this directory). */
export function detectGitSha(): string | null {
    if (env.gitSha) {
        return env.gitSha;
    }
    try {
        return execFileSync("git", ["rev-parse", "--short", "HEAD"], { cwd: INTEGRATION_ROOT, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim() || null;
    } catch {
        return null;
    }
}

/** Writes `reports/run.json`. Called once, from global setup. */
export function writeRunInfo(info: RunInfo): void {
    mkdirSync(dirname(RUN_INFO_PATH), { recursive: true });
    writeFileSync(RUN_INFO_PATH, JSON.stringify(info, null, 2), "utf8");
}

/** This invocation's identity, read fresh each call so a caller in the main process never sees a pre-setup value. */
export function currentRun(): RunInfo {
    try {
        if (existsSync(RUN_INFO_PATH)) {
            const info = JSON.parse(readFileSync(RUN_INFO_PATH, "utf8")) as Partial<RunInfo>;
            if (typeof info.runId === "string" && typeof info.startedAt === "string") {
                return { runId: info.runId, startedAt: info.startedAt, gitSha: info.gitSha ?? null, baseUrl: info.baseUrl ?? env.baseUrl };
            }
        }
    } catch {
        // Unreadable means global setup did not run here; fall through to this process's own identity.
    }
    return { runId: env.runId, startedAt: `process-${process.pid}`, gitSha: env.gitSha, baseUrl: env.baseUrl };
}

/** The run id shared by every worker of this invocation. */
export function currentRunId(): string {
    return currentRun().runId;
}

/** A key unique to this invocation, even when `UL_E2E_RUN_ID` is pinned across several. */
export function currentRunKey(): string {
    const run = currentRun();
    return `${run.runId}@${run.startedAt}`;
}

interface StoredValue<T> {
    runKey: string;
    writtenAt: string;
    value: T;
}

/** A JSON value shared between workers of one invocation and ignored by every later one. */
export class RunScopedStore<T> {
    readonly path: string;

    constructor(name: string) {
        this.path = resolve(REPORTS_DIR, "run-state", `${name}.json`);
    }

    /** The value this invocation wrote, or null. */
    read(): T | null {
        try {
            const stored = JSON.parse(readFileSync(this.path, "utf8")) as StoredValue<T>;
            return stored.runKey === currentRunKey() ? stored.value : null;
        } catch {
            return null;
        }
    }

    /** Records `value` for the rest of this invocation. Best effort: a failed write costs time, not correctness. */
    write(value: T): void {
        try {
            mkdirSync(dirname(this.path), { recursive: true });
            const stored: StoredValue<T> = { runKey: currentRunKey(), writtenAt: new Date().toISOString(), value };
            writeFileSync(this.path, JSON.stringify(stored), "utf8");
        } catch {
            // Deliberately swallowed; see the docstring.
        }
    }
}
