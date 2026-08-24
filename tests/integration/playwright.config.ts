/**
 * Playwright configuration for the on-demand integration suite.
 *
 * This suite is never pointed at a server it started itself - there is no
 * `webServer` block, deliberately. It runs against a deployment that is already
 * up, with its real database, real Valkey, real Celery workers, real proxy and
 * real TLS, because the failures it exists to catch live in the wiring between
 * those and not in any one of them.
 *
 * Projects are the unit of selection. `--project=smoke` is the five-second
 * question "is this deployment alive"; `--project=ui` is the long one. The
 * cross-browser and visual projects are registered only when asked for, so the
 * default `playwright test` does the useful thing rather than the exhaustive
 * one.
 */

import { defineConfig, devices, type Project } from "@playwright/test";

import { PRIMARY_ROLE, storageStatePath } from "./lib/accounts.js";
import { env } from "./lib/env.js";

/** Settings every browser project shares. */
const browserDefaults = {
    baseURL: env.baseUrl,
    ignoreHTTPSErrors: env.ignoreHttpsErrors,
    // Kept for the first retry rather than always: a trace is tens of megabytes
    // and the run that matters is the one that failed twice.
    trace: "on-first-retry" as const,
    video: "retain-on-failure" as const,
    screenshot: "only-on-failure" as const,
    actionTimeout: env.actionTimeoutMs,
    navigationTimeout: env.navigationTimeoutMs,
    // Both exist for debugging one failing spec by watching it happen.
    headless: !env.headed,
    launchOptions: { slowMo: env.slowMoMs },
    // Fixed rather than inherited from the machine running the suite, so a
    // date-formatting assertion means the same thing on a laptop and in CI.
    locale: "en-US",
    timezoneId: "America/New_York",
};

/** Signed in as the primary account, via the state the setup project saved. */
const signedIn = {
    ...browserDefaults,
    storageState: storageStatePath(PRIMARY_ROLE),
};

const projects: Project[] = [
    {
        name: "setup",
        testDir: "./setup",
        testMatch: /.*\.setup\.ts$/,
        use: { ...devices["Desktop Chrome"], ...browserDefaults },
    },
    {
        // Fast, mostly read-only, and the thing to run first: if this is red,
        // nothing else's failure means anything.
        name: "smoke",
        testDir: "./specs/smoke",
        dependencies: ["setup"],
        use: { ...devices["Desktop Chrome"], ...signedIn },
    },
    {
        // The dependencies rather than the application: database, cache,
        // WebSockets, background workers, static assets, third-party origins.
        name: "services",
        testDir: "./specs/services",
        dependencies: ["setup"],
        use: { ...devices["Desktop Chrome"], ...signedIn },
    },
    {
        // No browser and no session - these authenticate with API keys, so they
        // do not wait on the setup project and run at full speed.
        name: "api",
        testDir: "./specs/api",
        use: { ...browserDefaults },
    },
    {
        name: "ui",
        testDir: "./specs/ui",
        dependencies: ["setup"],
        use: { ...devices["Desktop Chrome"], ...signedIn },
    },
    {
        name: "a11y",
        testDir: "./specs/a11y",
        dependencies: ["setup"],
        use: { ...devices["Desktop Chrome"], ...signedIn },
    },
];

if (env.runCrossBrowser) {
    projects.push(
        { name: "ui-firefox", testDir: "./specs/ui", dependencies: ["setup"], use: { ...devices["Desktop Firefox"], ...signedIn } },
        { name: "ui-webkit", testDir: "./specs/ui", dependencies: ["setup"], use: { ...devices["Desktop Safari"], ...signedIn } },
        { name: "ui-mobile", testDir: "./specs/ui", dependencies: ["setup"], use: { ...devices["Pixel 7"], ...signedIn } },
    );
}

if (env.runVisual) {
    projects.push({
        name: "visual",
        testDir: "./specs/visual",
        dependencies: ["setup"],
        use: { ...devices["Desktop Chrome"], ...signedIn },
    });
}

export default defineConfig({
    globalSetup: "./setup/global-setup.ts",
    outputDir: "./reports/artifacts",
    fullyParallel: true,
    // A `.only` left in a spec silently reduces a run to one test. Harmless on
    // a laptop, dangerous when the run is the gate on a deploy.
    forbidOnly: !!process.env.CI,
    retries: env.retries,
    workers: env.workers,
    timeout: env.testTimeoutMs,
    expect: { timeout: env.expectTimeoutMs },
    reporter: [
        ["list"],
        ["html", { outputFolder: "reports/html", open: "never" }],
        ["junit", { outputFile: "reports/junit.xml" }],
        ["json", { outputFile: "reports/results.json" }],
    ],
    // Surfaced at the top of the HTML report, so a report that gets passed
    // around says which deployment produced it.
    metadata: {
        target: env.baseUrl,
        runId: env.runId,
        crossBrowser: env.runCrossBrowser,
        visual: env.runVisual,
    },
    use: browserDefaults,
    projects,
});
