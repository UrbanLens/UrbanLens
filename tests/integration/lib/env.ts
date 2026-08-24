/**
 * Configuration for a run, read once from the environment and validated eagerly.
 *
 * Everything the suite needs to know about *which* deployment it is pointed at
 * lives here. Reading it in one place means a misconfigured run fails with one
 * actionable message before a browser starts, rather than as a wall of
 * identical timeouts thirty seconds in.
 *
 * @see {@link ./accounts.ts} for the credentials half of the configuration.
 */

import { readFileSync, existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const INTEGRATION_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

/**
 * Hosts that are refused by default.
 *
 * These tests create, edit and delete real rows as a real account. Pointed at
 * production that is data loss, and the mistake is one environment variable
 * wide - so the denylist is on by default rather than being a flag somebody
 * remembers to set. Matching is on the exact hostname, never a substring:
 * `s1.dev.urbanlens.org` must not be caught by an entry for `urbanlens.org`.
 */
const DEFAULT_PRODUCTION_HOSTS = ["urbanlens.org", "www.urbanlens.org", "app.urbanlens.org"];

/** Loads `KEY=value` pairs from a dotenv file without taking a dependency. */
function loadDotEnv(path: string): void {
    if (!existsSync(path)) {
        return;
    }
    for (const rawLine of readFileSync(path, "utf8").split(/\r?\n/)) {
        const line = rawLine.trim();
        if (!line || line.startsWith("#")) {
            continue;
        }
        const separator = line.indexOf("=");
        if (separator === -1) {
            continue;
        }
        const key = line.slice(0, separator).trim();
        let value = line.slice(separator + 1).trim();
        if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
            value = value.slice(1, -1);
        }
        // A real environment variable always wins, so `UL_E2E_BASE_URL=... npm test`
        // overrides a stale .env rather than being silently ignored by it.
        if (process.env[key] === undefined) {
            process.env[key] = value;
        }
    }
}

loadDotEnv(resolve(INTEGRATION_ROOT, ".env"));

/** Thrown for a configuration problem, so the reporter can present it as such. */
export class ConfigurationError extends Error {
    constructor(message: string) {
        super(`Integration test configuration: ${message}`);
        this.name = "ConfigurationError";
    }
}

function readString(name: string, fallback?: string): string {
    const value = process.env[name]?.trim();
    if (value) {
        return value;
    }
    if (fallback !== undefined) {
        return fallback;
    }
    throw new ConfigurationError(`${name} is required. See tests/integration/.env.example.`);
}

function readBoolean(name: string, fallback: boolean): boolean {
    const value = process.env[name]?.trim().toLowerCase();
    if (!value) {
        return fallback;
    }
    if (["1", "true", "yes", "on"].includes(value)) {
        return true;
    }
    if (["0", "false", "no", "off"].includes(value)) {
        return false;
    }
    throw new ConfigurationError(`${name} must be a boolean-ish value, got "${value}".`);
}

function readInteger(name: string, fallback: number): number {
    const raw = process.env[name]?.trim();
    if (!raw) {
        return fallback;
    }
    const value = Number.parseInt(raw, 10);
    if (!Number.isFinite(value) || value < 0) {
        throw new ConfigurationError(`${name} must be a non-negative integer, got "${raw}".`);
    }
    return value;
}

function readList(name: string, fallback: string[]): string[] {
    const raw = process.env[name]?.trim();
    if (!raw) {
        return fallback;
    }
    return raw
        .split(",")
        .map((entry) => entry.trim().toLowerCase())
        .filter(Boolean);
}

/** Normalises a base URL to an origin plus optional path, with no trailing slash. */
function normaliseBaseUrl(raw: string): URL {
    let candidate = raw.trim();
    if (!/^https?:\/\//i.test(candidate)) {
        candidate = `https://${candidate}`;
    }
    let url: URL;
    try {
        url = new URL(candidate);
    } catch {
        throw new ConfigurationError(`UL_E2E_BASE_URL is not a URL: "${raw}".`);
    }
    url.hash = "";
    url.search = "";
    url.pathname = url.pathname.replace(/\/+$/, "");
    return url;
}

const baseUrl = normaliseBaseUrl(readString("UL_E2E_BASE_URL"));
const productionHosts = readList("UL_E2E_PRODUCTION_HOSTS", DEFAULT_PRODUCTION_HOSTS);
const allowProduction = readBoolean("UL_E2E_ALLOW_PRODUCTION", false);

if (productionHosts.includes(baseUrl.hostname.toLowerCase()) && !allowProduction) {
    throw new ConfigurationError(
        `refusing to run against "${baseUrl.hostname}", which is listed in UL_E2E_PRODUCTION_HOSTS. ` +
            "This suite writes and deletes data as a real account. Point UL_E2E_BASE_URL at staging.",
    );
}

/**
 * A run identifier stamped into every record the suite creates.
 *
 * Anything left behind by an interrupted run is greppable by this, and the
 * provisioning command's `--purge` can find it without guessing.
 */
const runId = readString("UL_E2E_RUN_ID", `${new Date().toISOString().replace(/[^0-9]/g, "").slice(0, 14)}`);

/**
 * The base URL as a string, guaranteed not to end in a slash.
 *
 * `URL.toString()` renders an empty path as `/`, so a plain `toString()` here
 * produces `http://host/` and every `${baseUrl}${path}` in the suite becomes
 * `http://host//path` - which a Django urlconf answers with a 404, on some
 * routes and not others, for no visible reason.
 */
const baseUrlString = baseUrl.toString().replace(/\/+$/, "");

export const env = {
    /** Origin (plus optional path prefix) of the deployment under test. */
    baseUrl: baseUrlString,
    /** Just the hostname, for messages and for the preflight's identity check. */
    host: baseUrl.hostname,
    /** Root of the versioned external API, absolute. */
    apiBaseUrl: `${baseUrlString}/dashboard/api/external/v1`,
    /** Origin for the WebSocket endpoints Channels serves. */
    websocketOrigin: `${baseUrl.protocol === "https:" ? "wss:" : "ws:"}//${baseUrl.host}`,

    runId,
    /**
     * Prefix on every name this suite writes, so leftovers are identifiable.
     *
     * Letters, digits and hyphens only. Every user-facing name in this
     * application passes through `sanitize_name` on save, which keeps a short
     * allowlist of everyday punctuation and drops the rest - so a prefix
     * wrapped in brackets comes back without them, and an assertion that the
     * name round-tripped fails on the suite's own decoration rather than on
     * anything the application did wrong.
     */
    resourcePrefix: `e2e-${runId}`,

    /** Self-signed or internal CA on a staging box - off by default. */
    ignoreHttpsErrors: readBoolean("UL_E2E_IGNORE_HTTPS_ERRORS", false),

    /**
     * Whether this deployment is expected to answer `/health/primary` with 200.
     * A read-replica site legitimately answers 503 there, so the assertion is
     * configuration rather than a fixed expectation.
     */
    expectPrimaryDatabase: readBoolean("UL_E2E_EXPECT_PRIMARY", true),

    /** Optional REData base URL. Unset skips the cross-service checks. */
    redataUrl: process.env.UL_E2E_REDATA_URL?.trim().replace(/\/+$/, "") || null,

    /** Opt-in project switches - see playwright.config.ts. */
    runVisual: readBoolean("UL_E2E_VISUAL", false),
    runCrossBrowser: readBoolean("UL_E2E_CROSS_BROWSER", false),

    /** Runner tuning. Defaults are chosen for a shared staging box, not a laptop. */
    workers: readInteger("UL_E2E_WORKERS", 4),
    retries: readInteger("UL_E2E_RETRIES", 1),
    testTimeoutMs: readInteger("UL_E2E_TIMEOUT_MS", 60_000),
    expectTimeoutMs: readInteger("UL_E2E_EXPECT_TIMEOUT_MS", 10_000),
    actionTimeoutMs: readInteger("UL_E2E_ACTION_TIMEOUT_MS", 15_000),
    navigationTimeoutMs: readInteger("UL_E2E_NAVIGATION_TIMEOUT_MS", 30_000),
    headed: readBoolean("UL_E2E_HEADED", false),
    slowMoMs: readInteger("UL_E2E_SLOW_MO_MS", 0),

    /**
     * Fail a UI test on any console error or failed subresource.
     *
     * On by default: "the page still rendered" is a weak assertion when a
     * script threw halfway through, and a broken bundle is exactly the class
     * of regression a staging run exists to catch.
     */
    strictConsole: readBoolean("UL_E2E_STRICT_CONSOLE", true),

    allowProduction,
    productionHosts,
} as const;

export type Env = typeof env;

/** Absolute URL for a site-relative path. */
export function siteUrl(path: string): string {
    return `${env.baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
}

/** Absolute URL for an external-API path, e.g. `apiUrl("pins/")`. */
export function apiUrl(path: string): string {
    return `${env.apiBaseUrl}/${path.replace(/^\/+/, "")}`;
}

/** Names a record this run created, so leftovers are traceable to it. */
export function resourceName(label: string): string {
    return `${env.resourcePrefix} ${label}`;
}
