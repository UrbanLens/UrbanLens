/**
 * The `test` object every spec imports.
 *
 * Specs should not construct clients, attach listeners, or remember to clean
 * up. Everything a spec needs arrives as a fixture, and everything a spec
 * creates is torn down whether it passed, failed, or timed out. That is what
 * makes it cheap to add the hundredth test rather than only the tenth.
 *
 * ```ts
 * import { expect, test } from "../../lib/fixtures.js";
 *
 * test("a pin the API created is visible on the map", async ({ page, api }) => {
 *     const pin = await api.createPin();          // deleted automatically
 *     await page.goto("/dashboard/map/");         // already signed in
 *     await expect(page.getByText(pin.name)).toBeVisible();
 * });                                             // console errors asserted here
 * ```
 */

import { test as base, expect, type APIRequestContext, type BrowserContext, type Page } from "@playwright/test";

import { optionalAccount, PRIMARY_ROLE, requireAccount, SECONDARY_ROLE, storageStatePath, type IntegrationAccount } from "./accounts.js";
import { ApiClient } from "./api-client.js";
import { env } from "./env.js";
import { installHtmxTracking } from "./htmx.js";
import { PageGuard } from "./page-guard.js";

export interface IntegrationOptions {
    /**
     * Fail a test whose page logged a console error or a failed subresource.
     *
     * On by default. A spec that deliberately provokes one narrows it with
     * `guard.allow(...)`; turn the whole check off only for a spec whose
     * subject *is* the error handling.
     */
    strictConsole: boolean;
}

export interface IntegrationFixtures {
    /** The account the browser context is signed in as. */
    account: IntegrationAccount;
    /** External-API client authenticated as {@link IntegrationFixtures.account}. */
    api: ApiClient;
    /** External-API client with no credentials, for authentication assertions. */
    anonymousApi: ApiClient;
    /**
     * External-API client holding the account's deliberately under-scoped key.
     *
     * Valid credential, insufficient grant - the only combination that
     * distinguishes working scope enforcement from an endpoint that happens to
     * be reachable.
     */
    restrictedApi: ApiClient;
    /**
     * External-API client acting as the `secondary` account.
     *
     * For the authorisation questions that need a second person: another
     * user's pin, another user's trip, a wiki nobody has earned.
     *
     * Fails loudly when no secondary account is configured. Gate the spec at
     * declaration time with {@link ifSecondaryAccount} - a fixture is built
     * before the test body runs, so a `test.skip()` inside the body is too
     * late to prevent it.
     */
    secondaryApi: ApiClient;
    /** Console/network watcher attached to `page`. Usually only touched to `allow()`. */
    guard: PageGuard;
    /**
     * A second signed-in page, as the `secondary` account.
     *
     * For anything involving two people: sharing, friend requests, messages,
     * a live update arriving in someone else's tab. Gate the spec with
     * {@link ifSecondaryAccount}, as for {@link IntegrationFixtures.secondaryApi}.
     */
    secondaryPage: Page;
}

export interface IntegrationWorkerFixtures {
    /**
     * A request context shared by every test in a worker.
     *
     * API calls do not need a browser, and building one per test would double
     * the run time of the API specs for nothing.
     */
    apiRequestContext: APIRequestContext;
}

/**
 * Guards, keyed by the page they watch.
 *
 * The `page` fixture attaches the guard and the `guard` fixture hands it out,
 * rather than the other way around. Attaching it inside `guard` and consuming
 * `guard` from `page` is the obvious arrangement and Playwright rejects it
 * outright as a fixture cycle - and it would be wrong anyway: a spec that never
 * mentions `guard` must still be watched, so the attachment cannot depend on
 * anyone asking for it.
 */
const guards = new WeakMap<Page, PageGuard>();

export const test = base.extend<IntegrationOptions & IntegrationFixtures, IntegrationWorkerFixtures>({
    strictConsole: [env.strictConsole, { option: true }],

    apiRequestContext: [
        async ({ playwright }, use) => {
            const context = await playwright.request.newContext({
                baseURL: env.baseUrl,
                ignoreHTTPSErrors: env.ignoreHttpsErrors,
                extraHTTPHeaders: {
                    Accept: "application/json",
                    // Makes this suite's traffic identifiable in access logs and
                    // in the per-key usage trail, so a staging run can be told
                    // apart from a real client misbehaving.
                    "User-Agent": `UrbanLens-Integration-Tests/${env.runId}`,
                },
            });
            await use(context);
            await context.dispose();
        },
        { scope: "worker" },
    ],

    account: async ({}, use) => {
        await use(requireAccount(PRIMARY_ROLE));
    },

    api: async ({ apiRequestContext, account }, use, testInfo) => {
        const client = new ApiClient(apiRequestContext, account.apiKey);
        await use(client);

        const leaks = await client.cleanup();
        if (leaks.length > 0) {
            // Reported, never thrown: a failed teardown must not turn a passing
            // test red, but a silent leak on a shared staging box is how the
            // next run ends up asserting against somebody else's rubbish.
            await testInfo.attach("cleanup-failures.txt", {
                body: `Could not remove ${leaks.length} resource(s):\n  ${leaks.join("\n  ")}`,
                contentType: "text/plain",
            });
        }
    },

    anonymousApi: async ({ apiRequestContext }, use) => {
        await use(new ApiClient(apiRequestContext, null));
    },

    restrictedApi: async ({ apiRequestContext, account }, use) => {
        await use(new ApiClient(apiRequestContext, account.restrictedApiKey));
    },

    secondaryApi: async ({ apiRequestContext }, use, testInfo) => {
        const secondary = requireAccount(SECONDARY_ROLE);
        const client = new ApiClient(apiRequestContext, secondary.apiKey);
        await use(client);

        const leaks = await client.cleanup();
        if (leaks.length > 0) {
            await testInfo.attach("cleanup-failures-secondary.txt", {
                body: `Could not remove ${leaks.length} resource(s):\n  ${leaks.join("\n  ")}`,
                contentType: "text/plain",
            });
        }
    },

    context: async ({ context }, use) => {
        await installHtmxTracking(context);
        await use(context);
    },

    // Overriding `page` rather than adding an auto fixture, so the guard is
    // attached before any navigation a spec makes and detached before teardown
    // navigations can add noise to it.
    page: async ({ page, strictConsole }, use, testInfo) => {
        const guard = PageGuard.attach(page);
        guards.set(page, guard);

        await use(page);

        guard.detach();
        if (!strictConsole) {
            return;
        }
        const report = guard.describe();
        if (report === null) {
            return;
        }
        // Only raised when the test otherwise passed. A test that already failed
        // has a better error, and burying it under a console dump helps nobody.
        if (testInfo.status === testInfo.expectedStatus) {
            throw new Error(`${report}\n\nSet test.use({ strictConsole: false }) or call guard.allow(...) if this is expected.`);
        }
        await testInfo.attach("page-problems.txt", { body: report, contentType: "text/plain" });
    },

    guard: async ({ page }, use) => {
        const guard = guards.get(page);
        if (!guard) {
            throw new Error("No guard is attached to this page. The `page` fixture attaches one; this means it was bypassed.");
        }
        await use(guard);
    },

    secondaryPage: async ({ browser }, use) => {
        // Specs gate themselves with `ifSecondaryAccount()`; this is the
        // backstop for one that forgot, and says what to do about it.
        requireAccount(SECONDARY_ROLE);

        let context: BrowserContext | null = null;
        try {
            context = await browser.newContext({
                baseURL: env.baseUrl,
                storageState: storageStatePath(SECONDARY_ROLE),
                ignoreHTTPSErrors: env.ignoreHttpsErrors,
            });
            await installHtmxTracking(context);
            const page = await context.newPage();
            await use(page);
        } finally {
            await context?.close();
        }
    },
});

/** Whether this run has an account for `role`. */
export function hasAccountFor(role: string): boolean {
    return optionalAccount(role) !== null;
}

/**
 * `test`, or a skipped `test`, depending on whether a second account exists.
 *
 * ```ts
 * ifSecondaryAccount()("another account's pin is not reachable", async ({ secondaryApi }) => { ... });
 * ```
 *
 * Declaration time rather than run time, deliberately. Playwright builds a
 * test's fixtures *before* its body runs, so a `test.skip()` inside the body
 * happens after `secondaryApi` has already tried - and failed - to resolve the
 * account it needs.
 */
export function ifSecondaryAccount(): typeof test | typeof test.skip {
    return hasAccountFor(SECONDARY_ROLE) ? test : test.skip;
}

export { expect };
export { PRIMARY_ROLE, SECONDARY_ROLE, STAFF_ROLE } from "./accounts.js";
export { env } from "./env.js";
