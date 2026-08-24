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
 * ## Why one worker
 *
 * The `location` project runs with a single worker (see `playwright.config.ts`).
 * These specs share one pin on one property, and the app enforces one root pin
 * per property per profile - so two workers racing to create it would have one
 * of them refused, and a worker deleting it in teardown would pull it out from
 * under the other. Parallelism here buys nothing anyway: the cost is waiting on
 * other people's APIs, not on CPU.
 */

import { type APIRequestContext } from "@playwright/test";

import { PRIMARY_ROLE, requireAccount } from "../../lib/accounts.js";
import { ApiClient } from "../../lib/api-client.js";
import { env } from "../../lib/env.js";
import { expect, test as suiteTest } from "../../lib/fixtures.js";
import { approximateAreaSqm, CAMPUS_CENTRE, EXPECTED_PARCEL_AREA_SQM, INSIDE_BOUNDARY, metresBetween, type Coordinate, type GeoJsonGeometry } from "../../lib/hrsh.js";
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

/**
 * Finds a pin this account already has on the campus, if any.
 *
 * A previous run's pin is reused rather than deleted and recreated: recreating
 * it would throw away the parcel geometry, building list and wiki enrichment
 * that took minutes to arrive, and would make every run pay for them again.
 */
async function findExistingCampusPin(api: ApiClient): Promise<CampusPin | null> {
    const page = await api.json<{ results?: Array<{ slug: string; latitude: number; longitude: number; parent_uuid?: string | null }> }>("get", "pins/", {
        // Generous: the account is disposable and holds few pins, and the
        // campus pin could be of any age.
        page_size: "200",
    } as unknown as undefined);

    for (const row of page.results ?? []) {
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
        const created = await api.json<{ slug: string }>("post", "pins/", {
            name: CAMPUS_PIN_NAME,
            latitude: origin.latitude,
            longitude: origin.longitude,
            description: `Created by the UrbanLens integration suite (run ${env.runId}).`,
            name_is_user_provided: true,
        });
        pin = await readPin(api, created.slug);
        note(`created ${pin.slug}`);
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
            diagnosis =
                `No parcel geometry arrived within ${BOUNDARY_WAIT_MS / 60_000} minutes. The pin's boundary is ` +
                (pin.boundary ? `a ${pin.boundary.type} of about ${area.toLocaleString()} m², which is the 50 m fallback circle rather than a parcel.` : "null.") +
                " Things worth checking, in the order they fail: the account's external_apis_enabled; UL_REDATA_API_URL/UL_REDATA_API_KEY on the deployment;" +
                " whether a Celery worker is consuming the default queue; and whether generate_boundaries_for_location ever ran for this Location.";
            note(diagnosis);
        }
    }

    return {
        api,
        pin,
        origin,
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

/** Skips the whole file unless this run opted into live location data. */
export function skipUnlessLocationDataEnabled(): void {
    locationDataTest.skip(
        !env.runLocationData,
        "Live location-data specs are off. They wait minutes on background enrichment and spend real third-party API calls. " +
            "Set UL_E2E_LOCATION_DATA=1, and provision the account with --external-apis, to run them.",
    );
}

export { expect };
