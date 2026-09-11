/**
 * The map's single-document fetch, against a real deployment.
 *
 * In the `ui` project rather than `api` because `map.document` is a
 * session-authenticated dashboard endpoint, and the `api` project deliberately
 * carries no session - it exists for the Bearer-token surface.
 *
 * Two things only an integration run can answer. Whether the copy Valkey hands
 * back is the same bytes the database produced - the unit tests compare a fake
 * cache, and a fake cannot get the encoding wrong the way a real client can.
 * And whether an account large enough to matter answers in a sane time, which
 * needs a real account, a real database and a real network between them.
 *
 * The size specs need a seeded account and skip without one:
 *
 *     manage.py provision_integration_env --roles primary,secondary,heavy --heavy-pins 30000
 */

import { env, expect, ifHeavyAccount, test } from "../../lib/fixtures.js";
import { mapDataRoutes } from "../../lib/routes.js";

import type { APIResponse, Page } from "@playwright/test";

/** Budgets, deliberately far above the measured numbers - see the note below. */
const HIT_BUDGET_MS = 1_000;
const MISS_BUDGET_MS = 8_000;

/**
 * How much faster a cached answer must be than an uncached one.
 *
 * The assertion that cannot be fooled by a slow machine: both halves are
 * measured in the same run on the same host, so the ratio holds where an
 * absolute millisecond budget is only ever a statement about one runner on one
 * day. Measured at 22x; three is the point below which the cache has plainly
 * stopped being used.
 */
const MIN_CACHE_SPEEDUP = 3;

/** Pins the heavy account must hold before the size specs mean anything. */
const HEAVY_PINS = 30_000;

/** How long to wait for the build task to populate the cache. */
const CACHE_BUILD_TIMEOUT_MS = 60_000;

interface Timed {
    response: APIResponse;
    elapsedMs: number;
}

async function timedGet(page: Page): Promise<Timed> {
    const started = Date.now();
    const response = await page.request.get(mapDataRoutes.document, { timeout: MISS_BUDGET_MS * 4 });
    // Bodies are read before the clock stops: a streamed response is not
    // finished when its headers arrive, and the headers are the cheap part.
    await response.body();
    return { response, elapsedMs: Date.now() - started };
}

function mode(response: APIResponse): string {
    return response.headers()["x-map-document"] ?? "";
}

/**
 * Poll until the build task has stored the document, or give up.
 *
 * `etag` matters more than it looks: these specs share one account and run in
 * parallel, so a sibling creating a pin moves the fingerprint mid-comparison and
 * the hit that arrives is of a *different* document. Accepting only the version
 * asked for is what makes the comparison sound without serialising the file.
 */
async function waitForCachedDocument(page: Page, etag?: string): Promise<Timed | null> {
    const deadline = Date.now() + CACHE_BUILD_TIMEOUT_MS;
    for (;;) {
        const attempt = await timedGet(page);
        const matches = etag === undefined || attempt.response.headers().etag === etag;
        if (mode(attempt.response) === "hit" && matches) return attempt;
        if (Date.now() > deadline) return null;
        await page.waitForTimeout(1_000);
    }
}

function documentLines(body: string): Array<Record<string, unknown>> {
    return body
        .split("\n")
        .filter((line) => line.length > 0)
        .map((line) => JSON.parse(line) as Record<string, unknown>);
}

test.describe("map document", () => {
    test("streams a head, a line per pin, and an end that agrees with the count", async ({ page, api }) => {
        await api.createPin();

        const { response } = await timedGet(page);

        expect(response.status()).toBe(200);
        expect(response.headers()["content-type"]).toContain("application/x-ndjson");
        const lines = documentLines(await response.text());
        expect(lines.at(0)?.t).toBe("head");
        const pins = lines.filter((line) => line.t === "pin");
        expect(lines.at(-1)).toEqual({ t: "end", sent: pins.length });
    });

    test("what Valkey hands back is what the database produced", async ({ page, api }) => {
        // Creating a pin moves the collection's fingerprint, so the next request
        // is a miss against a key nothing has written - which is the only way to
        // be sure the first of these two responses came from the database.
        //
        // Retried because the suite runs in parallel against one account: a
        // sibling spec can load the map between the write and the read, and warm
        // the very version this is trying to catch cold.
        let miss: Timed | null = null;
        for (let attempt = 0; attempt < 4 && miss === null; attempt += 1) {
            await api.createPin();
            const candidate = await timedGet(page);
            if (mode(candidate.response) === "miss") miss = candidate;
        }
        expect(miss, "never observed an uncached response; something else is warming this account").not.toBeNull();

        const etag = miss!.response.headers().etag;
        const streamed = await miss!.response.text();

        const hit = await waitForCachedDocument(page, etag);
        test.skip(
            hit === null,
            "no cached copy of this version arrived - either nothing is draining the Celery queue, or the account is being changed faster than it can be built",
        );

        expect(await hit!.response.text()).toBe(streamed);
    });

    test("an unchanged account revalidates with a 304", async ({ page }) => {
        // "Unchanged" is the precondition, and a parallel sibling can break it
        // between these two requests. Retried until the account holds still.
        let revalidated: APIResponse | null = null;
        for (let attempt = 0; attempt < 4 && revalidated?.status() !== 304; attempt += 1) {
            const etag = (await timedGet(page)).response.headers().etag ?? "";
            expect(etag, "every response should carry an ETag").toBeTruthy();
            revalidated = await page.request.get(mapDataRoutes.document, { headers: { "If-None-Match": etag } });
        }

        expect(revalidated?.status()).toBe(304);
    });

    test("signing out is refused", async ({ browser }) => {
        // An explicitly empty state, not an omitted one: this project signs every
        // context in by default, and omitting it inherits that.
        const anonymous = await browser.newContext({
            baseURL: env.baseUrl,
            ignoreHTTPSErrors: env.ignoreHttpsErrors,
            storageState: { cookies: [], origins: [] },
        });
        try {
            const response = await anonymous.request.get(mapDataRoutes.document, { maxRedirects: 0 });
            expect([301, 302, 403]).toContain(response.status());
        } finally {
            await anonymous.close();
        }
    });

    /**
     * Budgets rather than baselines. Measured over HTTP against a development
     * stack at 30,000 pins: an uncached document took 3,971 ms and a cached one
     * 179 ms. The absolute ceilings below leave roughly a factor of two on the
     * miss and five on the hit, because a shared runner's noise is larger than
     * the effect they guard against.
     *
     * The ratio is the real assertion. Both halves are measured in the same run
     * on the same host, so `MIN_CACHE_SPEEDUP` catches the regression that
     * matters - a cache that silently stopped being used - on a machine of any
     * speed. The millisecond ceilings are the coarse backstop for the other
     * kind: work that came back per pin.
     */
    ifHeavyAccount()("a large account answers within budget, cached and uncached", async ({ heavyPage }) => {
        test.slow();

        const first = await timedGet(heavyPage);
        const head = documentLines(await first.response.text()).at(0);
        const total = Number(head?.total ?? 0);
        test.skip(total < HEAVY_PINS, `the heavy account holds ${total} pins; re-provision with --heavy-pins ${HEAVY_PINS}`);
        // Exactly at the limit is served as one document; the branch that sends a
        // client back to the paged endpoint is one pin further on, and is
        // unit-tested. Asserted here rather than in its own spec because a
        // sibling reading this endpoint would warm the cache and the miss below
        // would never be measured.
        expect(head?.mode, "an account at the ceiling should still be served as one document").toBe("document");

        // Not forced. The obvious way to guarantee a miss is to create a pin,
        // but the account is seeded *at* the ceiling and one more tips it into
        // paged mode - which is the ceiling working, and useless for timing. A
        // freshly seeded account makes the first run a miss anyway.
        const uncached = mode(first.response) === "miss";
        if (uncached) {
            expect(first.elapsedMs, "uncached document").toBeLessThan(MISS_BUDGET_MS);
        }

        const cached = await waitForCachedDocument(heavyPage);
        test.skip(cached === null, "the cache never warmed - no Celery worker is draining this deployment's queue");

        expect(cached!.elapsedMs, "cached document").toBeLessThan(HIT_BUDGET_MS);
        if (uncached) {
            expect(cached!.elapsedMs, "the cached answer should be far faster than the uncached one").toBeLessThan(
                first.elapsedMs / MIN_CACHE_SPEEDUP,
            );
        }

        // Reported, not just asserted: a budget that passes says nothing about
        // how much room is left, and these are the numbers anyone adjusting them
        // would want.
        test.info().annotations.push({
            type: "measured",
            description:
                `${total} pins - ` +
                (uncached
                    ? `miss ${first.elapsedMs}ms (budget ${MISS_BUDGET_MS}ms), speedup ${(first.elapsedMs / cached!.elapsedMs).toFixed(1)}x, `
                    : "miss not exercised (cache was warm), ") +
                `hit ${cached!.elapsedMs}ms (budget ${HIT_BUDGET_MS}ms)`,
        });
    });
});
