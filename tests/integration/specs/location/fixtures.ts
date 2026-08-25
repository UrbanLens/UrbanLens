/**
 * Shared setup for the Hudson River State Hospital specs.
 *
 * Every spec in this directory needs the same thing first: one pin on the
 * campus, with its parcel geometry actually provisioned. That is slow (it waits
 * on county GIS data through REData), it costs real third-party calls, and it
 * is the same work for all of them - so it happens once per worker and is
 * shared.
 *
 * ## Why this fixture refuses to throw
 *
 * Provisioning can legitimately not happen: the account may have external
 * lookups off, the deployment may have no REData configured, or the pipeline
 * may be broken. If the fixture threw, all thirty-odd tests in this directory
 * would fail with the same setup error and the report would say nothing about
 * which of them would have passed.
 *
 * Instead it always yields, carrying either the geometry or a diagnosis of why
 * there is none. Exactly one spec - `hrsh-boundary.spec.ts` - asserts that
 * provisioning happened, so a broken pipeline produces **one** failure naming
 * the cause. Everything downstream calls {@link CampusFixture.requireBoundary}
 * and skips with a pointer to it. One red and thirty skips is a readable
 * report; thirty-one reds is not.
 *
 * ## Why one worker, and why "worker-scoped" is not the same as "once"
 *
 * The `location` project runs with a single worker (see `playwright.config.ts`).
 * These specs share one pin on one property, and the app enforces one root pin
 * per property per profile - so two workers racing to create it would have one
 * of them refused, and a worker deleting it in teardown would pull it out from
 * under the other. Parallelism here buys nothing anyway: the cost is waiting on
 * other people's APIs, not on CPU.
 *
 * That still does not make this setup run once. Playwright starts a **fresh
 * worker process per spec file** when the previous one is torn down, and each
 * fresh worker rebuilds its worker fixtures - so a seven-file directory ran the
 * ten-minute boundary wait seven times, turning a fifteen-minute run into an
 * hour. Measured, not theorised. The verdict is therefore cached on disk per run
 * id: the first worker waits, the rest re-read the pin once and take the
 * recorded answer. See {@link VERDICT_PATH}.
 */

import { type APIRequestContext, type Page } from "@playwright/test";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { PRIMARY_ROLE, requireAccount } from "../../lib/accounts.js";
import { ApiClient } from "../../lib/api-client.js";
import { env, INTEGRATION_ROOT } from "../../lib/env.js";
import { expect, test as suiteTest } from "../../lib/fixtures.js";
import { approximateAreaSqm, CAMPUS_CENTRE, EXPECTED_PARCEL_AREA_SQM, INSIDE_BOUNDARY, MEASURED_PARCEL_AREA_SQM, metresBetween, type Coordinate, type GeoJsonGeometry } from "../../lib/hrsh.js";
import { waitForOrNull } from "../../lib/waiting.js";

/** Name given to the campus pin, so a leftover is identifiable. */
const CAMPUS_PIN_NAME = "e2e Hudson River State Hospital";

/**
 * Radius within which an existing pin counts as "the campus pin", in metres.
 *
 * Wide enough to match a pin left by a previous run at any of the five campus
 * coordinates (the furthest is ~228 m from the centre), narrow enough not to
 * adopt something on a neighbouring property.
 */
const CAMPUS_MATCH_RADIUS_M = 400;

/**
 * How long to wait for parcel geometry to arrive.
 *
 * The chain is a Celery task calling REData, which in turn queries county GIS.
 * Ten minutes is generous to the point of being an upper bound rather than an
 * expectation - the intent is that a *timeout here means it is not coming*,
 * not that it was merely slow. A shorter wait would produce flaky failures that
 * get retried away, which is the outcome to avoid: this suite exists to notice
 * when the pipeline stops running.
 */
const BOUNDARY_WAIT_MS = 600_000;

/** How long a pin's own detail payload is polled while waiting. */
const BOUNDARY_POLL_INTERVAL_MS = 10_000;

/** The subset of the pin detail payload these specs read. */
export interface CampusPin {
    uuid: string;
    slug: string;
    name: string;
    location_slug: string;
    wiki_slug: string | null;
    boundary: GeoJsonGeometry | null;
}

export interface CampusFixture {
    /** The API client the campus pin belongs to. */
    api: ApiClient;
    /** The pin on the campus, always present - creating it does not depend on enrichment. */
    pin: CampusPin;
    /** Where the pin was placed. */
    origin: Coordinate;
    /**
     * The pin's name when setup finished.
     *
     * Recorded rather than assumed, because the fixture adopts a pin left by an
     * earlier run and that pin may carry any name. Asserting against a constant
     * would then fail on the inherited name rather than on a rename, which is
     * the opposite of what the test is for.
     */
    nameAtSetup: string;
    /** The resolved parcel geometry, or null when it never arrived. */
    boundary: GeoJsonGeometry | null;
    /** Why there is no boundary, when there is none. Empty string otherwise. */
    diagnosis: string;
    /**
     * Timestamped record of what setup did and saw.
     *
     * Carried rather than attached because a worker fixture has no `attach` -
     * only a test does. `hrsh-boundary.spec.ts` attaches it, which is also the
     * right place: that is the test whose failure it explains.
     */
    log: string;
    /** Metres from the campus pin to another coordinate. */
    metresFromFirstPin: (point: Coordinate) => number;
    /**
     * Skips the calling test when no parcel geometry was provisioned.
     *
     * Call this first in any test whose subject depends on the parcel. The skip
     * reason names `hrsh-boundary.spec.ts`, which is the one test that reports
     * the absence as a failure.
     */
    requireBoundary: () => void;
}

/** Fetches the pin detail payload these specs read repeatedly. */
async function readPin(api: ApiClient, slug: string): Promise<CampusPin> {
    return api.json<CampusPin>("get", `pins/${slug}/`);
}

/** One row of the pin delta-sync payload, as far as these specs read it. */
export interface SyncPinRow {
    slug: string;
    uuid: string;
    name: string;
    latitude: number;
    longitude: number;
    parent_uuid?: string | null;
}

/**
 * Every pin this account holds, root and child alike.
 *
 * `GET pins/` is a **delta-sync** endpoint, not an ordinary list: it answers
 * `{pins, next_cursor, sync_watermark, total}`, pages by opaque cursor, and
 * serves child pins alongside root ones. Reading `results` from it - the shape
 * every other list endpoint in this API uses - silently yields nothing, which
 * is exactly the sort of quiet wrong answer that makes a fixture look like an
 * application failure.
 */
export async function allPins(api: ApiClient): Promise<SyncPinRow[]> {
    const rows: SyncPinRow[] = [];
    let cursor: string | null = null;
    // Bounded rather than `while (true)`: a cursor that stopped advancing would
    // otherwise spin here forever instead of failing.
    for (let page = 0; page < 20; page += 1) {
        const params: Record<string, string> = { limit: "200" };
        if (cursor) {
            params.cursor = cursor;
        }
        const body: { pins?: SyncPinRow[]; next_cursor?: string | null } = await api.json("get", "pins/", params as never);
        rows.push(...(body.pins ?? []));
        cursor = body.next_cursor ?? null;
        if (!cursor) {
            break;
        }
    }
    return rows;
}

/**
 * Finds a pin this account already has on the campus, if any.
 *
 * A previous run's pin is reused rather than deleted and recreated: recreating
 * it would throw away the parcel geometry, building list and wiki enrichment
 * that took minutes to arrive, and would make every run pay for them again.
 */
async function findExistingCampusPin(api: ApiClient): Promise<CampusPin | null> {
    for (const row of await allPins(api)) {
        if (row.parent_uuid) {
            continue;
        }
        const distance = metresBetween(CAMPUS_CENTRE, { label: "candidate", latitude: row.latitude, longitude: row.longitude });
        if (distance <= CAMPUS_MATCH_RADIUS_M) {
            return await readPin(api, row.slug);
        }
    }
    return null;
}

/**
 * Whether a boundary payload is a real parcel rather than the fallback circle.
 *
 * `Boundary.objects.effective_polygon_for_pin` falls back to a 50 m circle when
 * nothing better is known, and that circle is served in the same field with the
 * same shape - so "boundary is not null" is not evidence of anything. A 50 m
 * circle is about 7,850 m²; the smallest parcel this could plausibly be is
 * 200,000 m². The gap is wide enough that area alone tells them apart.
 */
function isRealParcel(geometry: GeoJsonGeometry | null | undefined): boolean {
    return geometry != null && approximateAreaSqm(geometry) > EXPECTED_PARCEL_AREA_SQM.min;
}

/**
 * Where the parcel verdict is recorded so only one worker pays for it.
 *
 * The wait below is up to ten minutes and it is worker-scoped - which sounds
 * like "once" and is not. Playwright starts a **fresh worker process per spec
 * file**, and each one rebuilds its worker fixtures, so an eight-file directory
 * ran that wait eight times and turned a fifteen-minute run into ninety.
 * Measured, not theorised.
 *
 * The obvious key would be the run id. It does not work: `lib/env.ts` derives
 * `runId` from the clock when `UL_E2E_RUN_ID` is unset, and **every worker
 * computes its own** - Playwright snapshots the environment before loading the
 * config, so setting the variable there never reaches a worker (verified by
 * reading `/proc/<worker>/environ`). Keying on `env.runId` therefore gives every
 * worker its own cache file and no hits at all.
 *
 * So the key is a fixed path and freshness is a timestamp inside the file. Any
 * verdict written in the last {@link VERDICT_TTL_MS} belongs to the run in
 * progress; anything older is a previous run's and is ignored, which is what
 * stops a stale "no parcel" verdict from suppressing a real one tomorrow.
 *
 * Note this also means `env.resourcePrefix` differs per worker, so the suite's
 * documented promise that a run's leftovers are greppable by run id does not
 * currently hold - a separate defect, recorded in docs/INTEGRATION_TESTS.md.
 */
const VERDICT_PATH = resolve(INTEGRATION_ROOT, "reports", "campus-verdict.json");

/**
 * How long a recorded verdict is treated as belonging to the current run.
 *
 * Comfortably longer than one worker's wait plus the tests that follow it, and
 * far shorter than the gap between deliberate runs.
 */
const VERDICT_TTL_MS = 45 * 60 * 1000;

interface CachedVerdict {
    writtenAt: number;
    settled: boolean;
    diagnosis: string;
}

/** The verdict from this run, or null when there is none fresh enough. */
function readVerdict(): CachedVerdict | null {
    try {
        if (!existsSync(VERDICT_PATH)) {
            return null;
        }
        const verdict = JSON.parse(readFileSync(VERDICT_PATH, "utf8")) as CachedVerdict;
        return Date.now() - verdict.writtenAt < VERDICT_TTL_MS ? verdict : null;
    } catch {
        // A corrupt or half-written marker must not fail the run - the only cost
        // of ignoring it is that this worker waits like the first one did.
        return null;
    }
}

function writeVerdict(verdict: Omit<CachedVerdict, "writtenAt">): void {
    try {
        mkdirSync(dirname(VERDICT_PATH), { recursive: true });
        writeFileSync(VERDICT_PATH, JSON.stringify({ ...verdict, writtenAt: Date.now() }), "utf8");
    } catch {
        // Best effort. Failing to cache costs time, never correctness.
    }
}

/**
 * Creates or adopts the campus pin and waits for its parcel geometry.
 *
 * @param request A Playwright request context.
 * @returns Everything the specs in this directory share.
 */
async function buildCampus(request: APIRequestContext): Promise<CampusFixture> {
    const account = requireAccount(PRIMARY_ROLE);
    const api = new ApiClient(request, account.apiKey);
    const log: string[] = [];
    const note = (line: string) => log.push(`[${new Date().toISOString()}] ${line}`);

    const origin = INSIDE_BOUNDARY[0]!;
    let pin = await findExistingCampusPin(api);
    if (pin) {
        note(`adopted an existing campus pin: ${pin.slug}`);
    } else {
        note(`creating a campus pin at ${origin.latitude}, ${origin.longitude}`);
        const response = await api.post("pins/", {
            name: CAMPUS_PIN_NAME,
            latitude: origin.latitude,
            longitude: origin.longitude,
            description: `Created by the UrbanLens integration suite (run ${env.runId}).`,
            name_is_user_provided: true,
        });
        if (response.ok()) {
            const created = (await response.json()) as { slug: string };
            pin = await readPin(api, created.slug);
            note(`created ${pin.slug}`);
        } else {
            // A refusal here means a pin is already there and the search above
            // did not recognise it - a pin just outside CAMPUS_MATCH_RADIUS_M,
            // or one left by something other than this suite. Adopting it is
            // right either way: the app has just told us this coordinate is
            // taken, so the pin that holds it is the campus pin.
            const refusal = (await response.text()).slice(0, 200);
            note(`create refused (${response.status()}): ${refusal} - re-searching`);
            pin = await findExistingCampusPin(api);
            if (pin === null) {
                throw new Error(
                    `Could not create a pin on the campus (${refusal}) and could not find the pin that is blocking it. ` +
                        "Something else on this account holds these coordinates; list the account's pins and remove it, or run " +
                        "provision_integration_env --purge.",
                );
            }
            note(`adopted ${pin.slug} after the refusal`);
        }
    }

    // The documented lazy trigger. `services.pins.external_data`'s boundary
    // panel source is what runs the provider chain: nothing else does it for a
    // pin, by design ("the lazy path that replaced eager generation on pin
    // creation"). Asking for it is therefore part of setup, not part of any
    // assertion.
    const panel = await api.get(`pins/${pin.slug}/panels/boundary/`);
    note(`boundary panel: HTTP ${panel.status()} ${(await panel.text()).slice(0, 200)}`);

    let diagnosis = "";
    let boundary: GeoJsonGeometry | null = null;

    if (isRealParcel(pin.boundary)) {
        boundary = pin.boundary;
        note("parcel geometry was already present");
    } else if (readVerdict() !== null) {
        // Another worker in this run already waited it out. Re-read once in case
        // the geometry landed since, then take the recorded answer rather than
        // spending the ten minutes again.
        const cached = readVerdict()!;
        const fresh = await readPin(api, pin.slug);
        if (isRealParcel(fresh.boundary)) {
            pin = fresh;
            boundary = fresh.boundary;
            note("parcel geometry had arrived since an earlier worker looked");
        } else {
            diagnosis = cached.diagnosis;
            note("using the verdict an earlier worker in this run already reached");
        }
    } else {
        note("waiting for parcel geometry...");
        const settled = await waitForOrNull(
            () => readPin(api, pin!.slug),
            (value) => isRealParcel(value.boundary),
            {
                what: "the parcel boundary for the campus pin",
                timeoutMs: BOUNDARY_WAIT_MS,
                intervalMs: BOUNDARY_POLL_INTERVAL_MS,
                describe: (value) =>
                    value.boundary
                        ? `a ${value.boundary.type} of about ${Math.round(approximateAreaSqm(value.boundary)).toLocaleString()} m²`
                        : "boundary: null",
            },
        );
        if (settled) {
            pin = settled;
            boundary = settled.boundary;
            note("parcel geometry arrived");
        } else {
            const area = pin.boundary ? Math.round(approximateAreaSqm(pin.boundary)) : 0;
            // States what was observed and lets the reader draw the conclusion.
            // An earlier version asserted the polygon "is the 50 m fallback
            // circle", which was simply false - the pin had a 154,844 m²
            // boundary from a Boundary row - and sent the investigation after
            // the wrong thing. Say the number; do not name a cause.
            diagnosis =
                `No parcel geometry passed the size check within ${BOUNDARY_WAIT_MS / 60_000} minutes. The pin's boundary is ` +
                (pin.boundary
                    ? `a ${pin.boundary.type} of about ${area.toLocaleString()} m², outside the ${EXPECTED_PARCEL_AREA_SQM.min.toLocaleString()}-${EXPECTED_PARCEL_AREA_SQM.max.toLocaleString()} m² range this place is expected to fall in (REData measures the parcel at ${MEASURED_PARCEL_AREA_SQM.toLocaleString()} m²).`
                    : "null.") +
                " Two quite different things produce this and they are worth separating: the boundary chain never running at all, and it" +
                " running but resolving something the wrong size. Check, in order: the account's external_apis_enabled;" +
                " UL_REDATA_API_URL/UL_REDATA_API_KEY on the deployment; whether a Celery worker consumes the default queue; and whether" +
                " Location.place_id is set - a boundary can exist as a Boundary row while place resolution has never happened, which is a" +
                " different defect with the same appearance here.";
            note(diagnosis);
        }
        writeVerdict({ settled: boundary !== null, diagnosis });
    }

    return {
        api,
        pin,
        origin,
        nameAtSetup: pin.name,
        boundary,
        diagnosis,
        log: log.join("\n"),
        metresFromFirstPin: (point: Coordinate) => metresBetween(origin, point),
        requireBoundary: () => {
            suiteTest.skip(
                boundary === null,
                "no parcel geometry was provisioned for the campus, so this cannot be assessed. " +
                    "See the failure in hrsh-boundary.spec.ts, which reports that as the finding it is.",
            );
        },
    };
}

/**
 * `test` for this directory: campus fixture attached, skipped unless opted in.
 *
 * These specs are not part of an ordinary run. They wait minutes on background
 * work and they cost real third-party API calls against REData, county GIS and
 * Wikipedia - so they are off unless `UL_E2E_LOCATION_DATA` says otherwise, and
 * they skip loudly rather than failing when the account cannot make those calls.
 */
export const locationDataTest = suiteTest.extend<{}, { campus: CampusFixture }>({
    campus: [
        async ({ playwright }, use) => {
            const request = await playwright.request.newContext({
                baseURL: env.baseUrl,
                ignoreHTTPSErrors: env.ignoreHttpsErrors,
                extraHTTPHeaders: {
                    Accept: "application/json",
                    "User-Agent": `UrbanLens-Integration-Tests/${env.runId}`,
                },
            });
            try {
                await use(await buildCampus(request));
                // The pin is deliberately *not* deleted. Provisioning it costs
                // minutes and real API calls, and the next run adopts it - see
                // findExistingCampusPin. `provision_integration_env --purge`
                // removes the account and everything on it, which is the right
                // place for that cleanup.
            } finally {
                await request.dispose();
            }
        },
        { scope: "worker" },
    ],
});

/**
 * Ensures the campus has a real (promoted) wiki, and reports whether it does.
 *
 * Needed by more than one spec, because several endpoints that look like
 * property routes are actually **wiki** routes - `wikis/{slug}/ownership/` and
 * `wikis/{slug}/sales/` among them. Until a wiki is promoted those answer 404,
 * and a spec that waits on them without creating one first is not waiting for
 * enrichment at all; it is waiting for something that will never happen.
 *
 * Promotion has exactly one entry point in the product and it is not in the
 * published API: `POST /dashboard/map/pin/<slug>/wiki/create/`, from a browser
 * session. Hence the `page` argument.
 *
 * @param campus The campus fixture.
 * @param page A signed-in page, used for the CSRF token and the POST.
 * @returns True when a promoted wiki exists afterwards.
 */
export async function ensureCampusWiki(campus: CampusFixture, page: Page): Promise<boolean> {
    const existing = await campus.api.get(`wikis/${campus.pin.location_slug}/`);
    if (existing.status() === 200) {
        return true;
    }
    await page.goto(`/dashboard/map/pin/${campus.pin.slug}/`);
    const token = await page.evaluate(() => (document.querySelector('input[name="csrfmiddlewaretoken"]') as HTMLInputElement | null)?.value ?? "");
    const response = await page.request.post(`/dashboard/map/pin/${campus.pin.slug}/wiki/create/`, {
        headers: token ? { "X-CSRFToken": token } : {},
    });
    if (!response.ok()) {
        return false;
    }
    return (await campus.api.get(`wikis/${campus.pin.location_slug}/`)).status() === 200;
}

/** Skips the whole file unless this run opted into live location data. */
export function skipUnlessLocationDataEnabled(): void {
    locationDataTest.skip(
        !env.runLocationData,
        "Live location-data specs are off. They wait minutes on background enrichment and spend real third-party API calls. " +
            "Set UL_E2E_LOCATION_DATA=1, and provision the account with --external-apis, to run them.",
    );
}

export { expect };
