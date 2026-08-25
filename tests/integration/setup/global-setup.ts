/**
 * Preflight, run once before anything else.
 *
 * Its whole job is to turn "forty tests timed out" into one sentence saying
 * why. A deployment that is down, a URL with a typo in it, a manifest that was
 * never written, a proxy presenting a certificate the runner does not trust -
 * each of those produces an identical wall of timeouts thirty seconds in, and
 * each is diagnosable in under a second up front.
 *
 * It also prints what the run is pointed at. On a suite whose target is an
 * environment variable, "which deployment did that report come from" is a
 * question somebody always ends up asking.
 */

import { request, type FullConfig } from "@playwright/test";

import { allAccounts, reportedSiteUrl } from "../lib/accounts.js";
import { env } from "../lib/env.js";
import { publicRoutes } from "../lib/routes.js";

/** How long a healthy deployment gets to answer a liveness probe. */
const PROBE_TIMEOUT_MS = 20_000;

/** Body `/health/` and `/health/live` are documented to return. */
const LIVENESS_BODY = "Okay!";

export default async function globalSetup(_config: FullConfig): Promise<void> {
    const accounts = allAccounts();
    const roles = [...accounts.keys()];

    process.stdout.write(
        [
            "",
            "  UrbanLens integration suite",
            `  target      ${env.baseUrl}`,
            `  run id      ${env.runId}`,
            `  accounts    ${roles.join(", ")}`,
            `  workers     ${env.workers}   retries ${env.retries}`,
            "",
        ].join("\n") + "\n",
    );

    const context = await request.newContext({
        baseURL: env.baseUrl,
        ignoreHTTPSErrors: env.ignoreHttpsErrors,
        timeout: PROBE_TIMEOUT_MS,
    });

    try {
        await assertAlive(context);
        await assertReady(context);
        await assertIsUrbanLens(context);
        assertOriginIsTrusted();
    } finally {
        await context.dispose();
    }
}

/**
 * The URL we are using is one the deployment knows itself by.
 *
 * Django's CSRF check trusts only origins derived from `ALLOWED_HOSTS` and
 * `UL_SITE_URL`. Reaching the same instance by a different origin - through a
 * published container port rather than its hostname, say - leaves every page
 * rendering perfectly and every POST refused with a 403, sign-in included. That
 * presents as "the login form does nothing", which is a long way from its cause.
 *
 * The provisioning manifest already carries the deployment's own `SITE_URL`, so
 * this costs nothing and turns a whole run's worth of confusing failures into
 * one sentence.
 */
function assertOriginIsTrusted(): void {
    const reported = reportedSiteUrl();
    if (!reported) {
        return;
    }

    let expected: URL;
    try {
        expected = new URL(reported);
    } catch {
        return;
    }
    const actual = new URL(env.baseUrl);
    if (expected.origin === actual.origin) {
        return;
    }

    throw new Error(
        `UL_E2E_BASE_URL is ${actual.origin}, but the deployment reports its own site URL as ${expected.origin}.\n` +
            "Django trusts only the origins it was configured for, so every POST this suite makes - sign-in included - would be refused with a CSRF 403 while every page still rendered.\n" +
            `Point UL_E2E_BASE_URL at ${expected.origin}, or set UL_SITE_URL on the deployment to the URL you are reaching it by.`,
    );
}

/** The process answers at all, with a message that names the likely cause. */
async function assertAlive(context: Awaited<ReturnType<typeof request.newContext>>): Promise<void> {
    let response;
    try {
        response = await context.get(publicRoutes.healthLive);
    } catch (error) {
        const message = (error as Error).message;
        const hint = /self.signed|unable to verify|certificate/i.test(message)
            ? " The certificate was not trusted - set UL_E2E_IGNORE_HTTPS_ERRORS=1 if this is a staging box with its own CA."
            : " Check UL_E2E_BASE_URL, and that the deployment is up and reachable from here.";
        throw new Error(`Could not reach ${env.baseUrl}${publicRoutes.healthLive}: ${message}.${hint}`);
    }

    if (!response.ok()) {
        throw new Error(`${env.baseUrl}${publicRoutes.healthLive} answered ${response.status()}. The application process is not serving.`);
    }
    const body = (await response.text()).trim();
    if (body !== LIVENESS_BODY) {
        // Almost always a proxy or captive portal answering instead of the app.
        throw new Error(`${env.baseUrl}${publicRoutes.healthLive} answered 200 but with "${body.slice(0, 80)}" rather than "${LIVENESS_BODY}". Something other than UrbanLens is serving this URL.`);
    }
}

/**
 * Dependencies are reachable.
 *
 * A 503 here is a legitimate answer from a deployment whose database or cache
 * is unwell, and the specs would then fail one by one with unrelated-looking
 * errors. Reporting which dependency is unwell is the whole value.
 */
async function assertReady(context: Awaited<ReturnType<typeof request.newContext>>): Promise<void> {
    const response = await context.get(publicRoutes.healthReady);
    const report = await response.json().catch(() => ({}) as Record<string, unknown>);

    if (!response.ok()) {
        throw new Error(`${env.baseUrl}${publicRoutes.healthReady} answered ${response.status()}: ${JSON.stringify(report)}. A dependency this suite needs is down.`);
    }

    // Advisory rather than fatal, exactly as the probe itself treats it: during
    // a rolling deploy an instance legitimately runs briefly behind its schema.
    if (report.migrations === "behind") {
        process.stdout.write("  warning     the target reports unapplied migrations; failures may be schema drift rather than regressions\n\n");
    }
    if (env.expectPrimaryDatabase && report.role === "replica") {
        throw new Error(
            `${env.baseUrl} is serving from a read-only replica, so every write this suite performs will fail. ` +
                "Point at the primary site, or set UL_E2E_EXPECT_PRIMARY=0 to run only the read-only projects.",
        );
    }
}

/** This is UrbanLens, and its login page renders. */
async function assertIsUrbanLens(context: Awaited<ReturnType<typeof request.newContext>>): Promise<void> {
    const response = await context.get(publicRoutes.login);
    if (!response.ok()) {
        throw new Error(`${env.baseUrl}${publicRoutes.login} answered ${response.status()}. The suite cannot sign in.`);
    }
    const html = await response.text();
    if (!html.includes('id="id_username"') || !html.includes('id="id_password"')) {
        throw new Error(
            `${env.baseUrl}${publicRoutes.login} does not look like the UrbanLens login form. ` +
                "Either something else is serving this hostname, or the form's field ids changed and setup/auth.setup.ts needs updating.",
        );
    }
}
