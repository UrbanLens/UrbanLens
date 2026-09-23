/**
 * The `test` object every spec imports. Specs should not construct clients, attach listeners, or
 * remember to clean up.
 */

import { test as base, expect, type APIRequestContext, type Browser, type Page, type TestInfo } from "@playwright/test";

import {
    hasFeature,
    HEAVY_ROLE,
    optionalAccount,
    PRIMARY_ROLE,
    PROPERTY_OWNERS_FEATURE,
    requireAccount,
    SECONDARY_ROLE,
    storageStatePath,
    SUBSCRIBER_ROLE,
    type IntegrationAccount,
} from "./accounts.js";
import { ApiClient } from "./api-client.js";
import { ConfigurationError, env } from "./env.js";
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
    /**
     * A signed-in page as the `heavy` account - the one seeded with a realistic
     * number of pins by `provision_integration_env --heavy-pins`.
     *
     * For the specs whose subject is size. Gate with {@link ifHeavyAccount}.
     */
    heavyPage: Page;
    /** External-API client as the `subscriber` account, which holds `property_owners`. Gate with {@link ifSubscriberAccount}. */
    subscriberApi: ApiClient;
    /** A signed-in page as the `subscriber` account. Gate with {@link ifSubscriberAccount}. */
    subscriberPage: Page;
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

/** Hands `use` a page signed in as `role`, and closes its context afterwards. */
async function withSignedInPage(browser: Browser, role: string, use: (page: Page) => Promise<void>): Promise<void> {
    const context = await browser.newContext({
        baseURL: env.baseUrl,
        storageState: storageStatePath(role),
        ignoreHTTPSErrors: env.ignoreHttpsErrors,
    });
    try {
        await installHtmxTracking(context);
        await use(await context.newPage());
    } finally {
        await context.close();
    }
}

/** Hands `use` an API client for `account` and reports anything it could not clean up. */
async function withAccountApi(request: APIRequestContext, account: IntegrationAccount, use: (client: ApiClient) => Promise<void>, testInfo: TestInfo): Promise<void> {
    const client = new ApiClient(request, account.apiKey);
    await use(client);
    const leaks = await client.cleanup();
    if (leaks.length > 0) {
        await testInfo.attach(`cleanup-failures-${account.role}.txt`, {
            body: `Could not remove ${leaks.length} resource(s):\n  ${leaks.join("\n  ")}`,
            contentType: "text/plain",
        });
    }
}

/** The subscriber account, refusing one the manifest says lacks the feature - that is a provisioning error, not a finding. */
function requireSubscriber(): IntegrationAccount {
    const account = requireAccount(SUBSCRIBER_ROLE);
    if (!hasFeature(account, PROPERTY_OWNERS_FEATURE)) {
        throw new ConfigurationError(
            `the "${SUBSCRIBER_ROLE}" account does not hold ${PROPERTY_OWNERS_FEATURE} (features: ${account.features.join(", ") || "none"}). ` +
                `Re-run "manage.py provision_integration_env" with --subscriber-roles ${SUBSCRIBER_ROLE}.`,
        );
    }
    return account;
}

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
        await withAccountApi(apiRequestContext, requireAccount(SECONDARY_ROLE), use, testInfo);
    },

    subscriberApi: async ({ apiRequestContext }, use, testInfo) => {
        await withAccountApi(apiRequestContext, requireSubscriber(), use, testInfo);
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

    heavyPage: async ({ browser }, use) => {
        requireAccount(HEAVY_ROLE);
        await withSignedInPage(browser, HEAVY_ROLE, use);
    },

    secondaryPage: async ({ browser }, use) => {
        // Specs gate themselves with `ifSecondaryAccount()`; this is the
        // backstop for one that forgot, and says what to do about it.
        requireAccount(SECONDARY_ROLE);
        await withSignedInPage(browser, SECONDARY_ROLE, use);
    },

    subscriberPage: async ({ browser }, use) => {
        requireSubscriber();
        await withSignedInPage(browser, SUBSCRIBER_ROLE, use);
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

/**
 * `test`, or a skipped `test`, depending on whether a seeded heavy account exists.
 *
 * Provision one with
 * `manage.py provision_integration_env --roles primary,secondary,heavy --heavy-pins 30000`.
 * Seeding takes minutes, so ordinary runs skip these rather than paying for them.
 */
export function ifHeavyAccount(): typeof test | typeof test.skip {
    return hasAccountFor(HEAVY_ROLE) ? test : test.skip;
}

/**
 * `test`, or a skipped `test`, depending on whether a `subscriber` account exists.
 *
 * Provision one with `--roles primary,secondary,subscriber --subscriber-roles subscriber`.
 */
export function ifSubscriberAccount(): typeof test | typeof test.skip {
    return hasAccountFor(SUBSCRIBER_ROLE) ? test : test.skip;
}

export { expect };
export { HEAVY_ROLE, PRIMARY_ROLE, PROPERTY_OWNERS_FEATURE, SECONDARY_ROLE, STAFF_ROLE, SUBSCRIBER_ROLE } from "./accounts.js";
export { env } from "./env.js";
