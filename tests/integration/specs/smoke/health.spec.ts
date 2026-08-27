/**
 * The three health probes, and the distinctions between them.
 *
 * These are the endpoints an orchestrator acts on: compose marks the `app`
 * container unhealthy on a non-2xx from `/health/`, which blocks Daphne from
 * starting; a load balancer pointed at `/health/primary` decides which site
 * receives writes. A probe that answers the wrong thing therefore does not
 * merely mis-report - it takes a site out, or sends writes to a replica.
 *
 * Their *differences* are what is asserted here. All three returning 200 on a
 * healthy deployment proves nothing about whether liveness would still answer
 * with a sick database, which is the entire reason the set was split.
 */

import { expect, test } from "../../lib/fixtures.js";
import { env } from "../../lib/env.js";
import { publicRoutes } from "../../lib/routes.js";

test.describe("health probes", () => {
    test("liveness answers plainly and without credentials", async ({ request }) => {
        for (const path of [publicRoutes.health, publicRoutes.healthLive]) {
            const response = await request.get(path);
            expect(response.status(), `${path} should be 200`).toBe(200);
            expect((await response.text()).trim()).toBe("Okay!");
        }
    });

    test("readiness reports every dependency it checked", async ({ request }) => {
        const response = await request.get(publicRoutes.healthReady);
        const report = (await response.json()) as Record<string, string>;

        // The body is a contract: an operator reads it to find out *which*
        // dependency is unwell, and a probe that reported only a status code
        // would send them to the logs instead.
        expect(Object.keys(report).sort()).toEqual(["cache", "db", "migrations", "role"]);
        expect(report.db, `readiness reported the database as "${report.db}"`).toBe("ok");
        expect(report.cache, `readiness reported the cache as "${report.cache}"`).toBe("ok");
        expect(["primary", "replica", "unknown"]).toContain(report.role);
        expect(response.status()).toBe(200);
    });

    test("migration state is reported, and is current on a settled deployment", async ({ request }) => {
        const response = await request.get(publicRoutes.healthReady);
        const report = (await response.json()) as Record<string, string>;

        // Advisory by design - an instance mid-rolling-deploy is legitimately
        // behind - so this reports rather than fails, except for `unknown`,
        // which means the check itself broke.
        expect(report.migrations).not.toBe("unknown");
        if (report.migrations === "behind") {
            test.info().annotations.push({ type: "warning", description: "The deployment has unapplied migrations." });
        }
    });

    test("the writable probe agrees with what this run expects", async ({ request }) => {
        const response = await request.get(publicRoutes.healthPrimary);
        const report = (await response.json()) as Record<string, string>;

        if (env.expectPrimaryDatabase) {
            expect(response.status(), `/health/primary answered ${response.status()} with ${JSON.stringify(report)}`).toBe(200);
            expect(report.role).toBe("primary");
        } else {
            // A replica site answering 503 here is the feature, not a fault:
            // it is how a load balancer keeps writes off a read-only database.
            expect(report.role).toBe("replica");
            expect(response.status()).toBe(503);
        }
    });

    test("probes stay reachable without a session", async ({ browser }) => {
        const anonymous = await browser.newContext({ storageState: { cookies: [], origins: [] }, ignoreHTTPSErrors: env.ignoreHttpsErrors });
        try {
            // Cloudflare, Kubernetes and compose all probe without credentials.
            // A probe that started requiring one would look healthy in a browser
            // and fail every real prober.
            const response = await anonymous.request.get(`${env.baseUrl}${publicRoutes.healthLive}`);
            expect(response.status()).toBe(200);
        } finally {
            await anonymous.close();
        }
    });
});
