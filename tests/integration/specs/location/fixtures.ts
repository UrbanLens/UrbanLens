/**
 * Shared setup for the Hudson River State Hospital specs: one pin at the requirement's coordinate,
 * enrichment started the way a user starts it (by opening the private pin page), and the parcel
 * waited for once per run. Reasoning lives in docs/LOCATION_DATA_TESTS.md.
 */

import { type APIRequestContext, type Browser, type Page, type PlaywrightWorkerArgs, type WorkerFixture } from "@playwright/test";

import { PRIMARY_ROLE, requireAccount, SECONDARY_ROLE, storageStatePath } from "../../lib/accounts.js";
import { ApiClient, ApiError } from "../../lib/api-client.js";
import { env } from "../../lib/env.js";
import { expect, test as suiteTest } from "../../lib/fixtures.js";
import {
    approximateAreaSqm,
    CAMPUS_CENTRE,
    COURTYARD_PIN,
    EXPECTED_PARCEL_AREA_SQM,
    HRSH_NAME_PATTERN,
    HRSH_PIN,
    hrshRoutes,
    MEASURED_PARCEL_AREA_SQM,
    metresBetween,
    type Coordinate,
    type GeoJsonGeometry,
} from "../../lib/hrsh.js";
import { installHtmxTracking, waitForHtmxSettled } from "../../lib/htmx.js";
import { readPageTimings, recordMetric, recordPageTimings, type MetricTags, type PageTimings } from "../../lib/metrics.js";
import { pinDetail } from "../../lib/routes.js";
import { RunScopedStore } from "../../lib/run.js";
import { waitFor, waitForOrNull, WaitTimeoutError } from "../../lib/waiting.js";

/** The campus pin's user-provided name. Contains no real name, so a correctly titled wiki cannot have copied it. */
export const CAMPUS_PRIVATE_NAME = "e2e private campus notes";

/** The courtyard pin's user-provided name, private for the same reason. */
export const COURTYARD_PRIVATE_NAME = "e2e private courtyard notes";

/**
 * One pinned point on the campus and the account that owns it.
 *
 * The application allows one root pin per property per account, so each site belongs to its own account.
 */
export interface SiteConfig {
    /** Short label, also the metric tag and run-state key suffix. */
    key: string;
    role: string;
    point: Coordinate;
    privateName: string;
}

export const CAMPUS_SITE: SiteConfig = { key: "campus", role: PRIMARY_ROLE, point: HRSH_PIN, privateName: CAMPUS_PRIVATE_NAME };
export const COURTYARD_SITE: SiteConfig = { key: "courtyard", role: SECONDARY_ROLE, point: COURTYARD_PIN, privateName: COURTYARD_PRIVATE_NAME };

/** Any root pin this close to the campus centre is the campus pin: covers every campus coordinate, not the neighbours. */
const CAMPUS_MATCH_RADIUS_M = 400;

/** An upper bound, not an expectation: a timeout here means the parcel is not coming. */
const BOUNDARY_WAIT_MS = 600_000;
const BOUNDARY_POLL_INTERVAL_MS = 10_000;

/** How long the page gets to make its own boundary request after DOMContentLoaded. */
const BOUNDARY_REQUEST_TIMEOUT_MS = 30_000;

/** Time on the page after HTMX settles, so `hx-trigger="load delay:2s"` panels fire as they would for a user. */
const TRIGGER_DWELL_MS = 5_000;

const DEFAULT_WIKI_WAIT_MS = 300_000;
const DEFAULT_CHILD_PIN_WAIT_MS = 300_000;

/** The subset of `GET pins/{slug}/` these specs read. */
export interface CampusPin {
    uuid: string;
    slug: string;
    name: string;
    latitude: number;
    longitude: number;
    location_slug: string;
    wiki_slug: string | null;
    parent_uuid?: string | null;
    boundary: GeoJsonGeometry | null;
}

/** One row of the `GET pins/` delta-sync payload. */
export interface SyncPinRow {
    slug: string;
    uuid: string;
    name: string;
    latitude: number;
    longitude: number;
    parent_uuid?: string | null;
    pin_type?: string | null;
}

/** The subset of `GET wikis/{location_slug}/` these specs read. */
export interface WikiDetail {
    location_slug: string;
    wiki_slug: string | null;
    uuid: string;
    name: string;
    description?: string | null;
    latitude: number | null;
    longitude: number | null;
    boundary: GeoJsonGeometry | null;
    aliases?: Array<{ name?: string; kind?: string }>;
    article?: unknown;
    created?: string;
    updated?: string;
}

/** A private pin page load. */
export interface PinPageLoad {
    status: number;
    timings: PageTimings;
}

/** The setup visit that started enrichment. */
export interface TriggerVisit {
    trigger: "private-pin-page";
    /** Document status, or null when navigation itself failed. */
    status: number | null;
    timings: PageTimings | null;
    /** Status of the page's own `/boundary/` request, or null when it never made one. */
    boundaryStatus: number | null;
    /** Navigation or settle failure, empty when there was none. */
    error: string;
}

export interface CampusFixture {
    /** Which pinned point this is. */
    site: SiteConfig;
    /** Client for the account that owns the pin. */
    api: ApiClient;
    pin: CampusPin;
    /** Where the pin actually is (an adopted pin may predate {@link HRSH_PIN}). */
    origin: Coordinate;
    /** The pin's name when the run's first setup finished; later workers reuse it, so a mid-run rename is detectable. */
    nameAtSetup: string;
    /** Whether `nameAtSetup` avoids every HRSH name. False for a pin adopted from an older run; UL_E2E_HRSH_FRESH=1 recreates it. */
    nameIsPrivate: boolean;
    /** Whether this run created the pin rather than adopting one. */
    created: boolean;
    /** The visit that started enrichment, or null if it never happened this run. */
    visit: TriggerVisit | null;
    /** The resolved parcel geometry, or null when it never arrived. */
    boundary: GeoJsonGeometry | null;
    /** Why there is no boundary, when there is none. Empty string otherwise. */
    diagnosis: string;
    /** Timestamped record of what setup did; attach it where it explains a failure. */
    log: string;
    metresFromFirstPin: (point: Coordinate) => number;
    /** Skips the calling test when no parcel geometry was provisioned; `hrsh-boundary.spec.ts` reports that as the failure. */
    requireBoundary: () => void;
}

/** What the run's first campus setup recorded, for workers started later in the same run. */
interface CampusRunState {
    pinSlug: string;
    created: boolean;
    nameAtSetup: string;
    visit: TriggerVisit | null;
    verdict: { settled: boolean; diagnosis: string } | null;
    log: string[];
}

const siteStates: Record<string, RunScopedStore<CampusRunState>> = {};

/** The run-scoped record for one site; the campus keeps its original key so resumed runs still find it. */
function siteState(site: SiteConfig): RunScopedStore<CampusRunState> {
    siteStates[site.key] ??= new RunScopedStore<CampusRunState>(site.key === "campus" ? "hrsh-campus" : `hrsh-${site.key}`);
    return siteStates[site.key]!;
}

/** Waits already run to their timeout this run, so a broken pipeline costs one timeout rather than one per test. */
const exhaustedWaits = new RunScopedStore<Record<string, string>>("hrsh-exhausted-waits");

function isExhausted(key: string): boolean {
    return Boolean(exhaustedWaits.read()?.[key]);
}

function markExhausted(key: string): void {
    exhaustedWaits.write({ ...(exhaustedWaits.read() ?? {}), [key]: new Date().toISOString() });
}

/** Reads one pin's detail payload. */
export async function readPin(api: ApiClient, slug: string): Promise<CampusPin> {
    return api.json<CampusPin>("get", `pins/${slug}/`);
}

/** Every pin this account holds, root and child alike. `GET pins/` is delta-sync: `{pins, next_cursor}`, not `results`. */
export async function allPins(api: ApiClient): Promise<SyncPinRow[]> {
    const rows: SyncPinRow[] = [];
    let cursor: string | null = null;
    for (let page = 0; page < 20; page += 1) {
        const params: Record<string, string> = { limit: "200" };
        if (cursor) {
            params.cursor = cursor;
        }
        const body: { pins?: SyncPinRow[]; next_cursor?: string | null } = await api.json("get", "pins/", params);
        rows.push(...(body.pins ?? []));
        cursor = body.next_cursor ?? null;
        if (!cursor) {
            break;
        }
    }
    return rows;
}

function onCampus(row: SyncPinRow): boolean {
    return !row.parent_uuid && metresBetween(CAMPUS_CENTRE, { label: row.slug, latitude: row.latitude, longitude: row.longitude }) <= CAMPUS_MATCH_RADIUS_M;
}

/** The account's root pin on the campus nearest the site's point, if any. */
async function findExistingCampusPin(api: ApiClient, point: Coordinate): Promise<CampusPin | null> {
    const candidates = (await allPins(api)).filter(onCampus);
    const distance = (row: SyncPinRow) => metresBetween(point, { label: row.slug, latitude: row.latitude, longitude: row.longitude });
    const nearest = candidates.sort((a, b) => distance(a) - distance(b))[0];
    return nearest ? readPin(api, nearest.slug) : null;
}

/** Deletes every root pin on the campus and their child pins (`?children=delete`, the website's own semantics). */
async function deleteCampusPins(api: ApiClient, note: (line: string) => void): Promise<void> {
    for (const row of (await allPins(api)).filter(onCampus)) {
        const response = await api.delete(`pins/${row.slug}/?children=delete`);
        note(`fresh: deleted ${row.slug} ("${row.name}") and its child pins: HTTP ${response.status()}`);
        if (!response.ok() && response.status() !== 404) {
            throw new ApiError("DELETE", `pins/${row.slug}/?children=delete`, response.status(), await response.text());
        }
    }
}

/** Whether a boundary is a parcel rather than the 50 m fallback circle (~7,850 m², far under the parcel floor). */
function isRealParcel(geometry: GeoJsonGeometry | null | undefined): boolean {
    return geometry != null && approximateAreaSqm(geometry) > EXPECTED_PARCEL_AREA_SQM.min;
}

/** Opens `/dashboard/map/pin/<slug>/` without waiting for network idle (the page holds a WebSocket open), and reads its timing. */
const SUBSCRIBER_PIN_NAME = "e2e subscriber ownership pin";
/** Root pins this close to HRSH_PIN are treated as the same private pin across runs. */
const PIN_MATCH_RADIUS_M = 400;

/** Finds this account's own root pin at {@link HRSH_PIN}, or creates one. Never deleted: the next run adopts it. */
export async function findOrCreateSubscriberPin(api: ApiClient): Promise<CampusPin> {
    const onCampus = (row: { parent_uuid?: string | null; slug: string; latitude: number; longitude: number }) =>
        !row.parent_uuid && metresBetween(HRSH_PIN, { label: row.slug, latitude: row.latitude, longitude: row.longitude }) <= PIN_MATCH_RADIUS_M;

    const existing = (await allPins(api)).filter(onCampus)[0];
    if (existing) {
        return readPin(api, existing.slug);
    }

    const response = await api.post("pins/", {
        name: SUBSCRIBER_PIN_NAME,
        latitude: HRSH_PIN.latitude,
        longitude: HRSH_PIN.longitude,
        description: `Created by the UrbanLens integration suite (run ${env.runId}) for the subscriber-side HRSH specs.`,
        name_is_user_provided: true,
    });
    if (response.ok()) {
        const created = (await response.json()) as { slug: string };
        return readPin(api, created.slug);
    }

    // Another worker created it between the list and this create; adopt it instead.
    const refusal = (await response.text()).slice(0, 200);
    const adopted = (await allPins(api)).filter(onCampus)[0];
    if (!adopted) {
        throw new ApiError("POST", "pins/", response.status(), `could not create the subscriber's pin at ${HRSH_PIN.latitude}, ${HRSH_PIN.longitude} (${refusal}) and found none to adopt.`);
    }
    return readPin(api, adopted.slug);
}

export async function openPrivatePin(page: Page, slug: string, options: { metricPrefix?: string | null; tags?: MetricTags; waitForLoadMs?: number } = {}): Promise<PinPageLoad> {
    const path = pinDetail(slug);
    const response = await page.goto(path, { waitUntil: "domcontentloaded" });
    const status = response?.status() ?? 0;
    const landed = new URL(page.url()).pathname;
    if (status < 200 || status >= 300 || landed !== path) {
        throw new Error(`Opening the private pin page ${path} answered HTTP ${status} and landed on ${landed}; expected HTTP 200 at ${path}.`);
    }
    const timings = await readPageTimings(page, options.waitForLoadMs);
    const prefix = options.metricPrefix === undefined ? "hrsh.pin_page" : options.metricPrefix;
    if (prefix) {
        recordPageTimings(timings, prefix, { tags: options.tags });
    }
    return { status, timings };
}

/** Opens the pin page as the owner would, waits for its boundary request and HTMX panels, and reports what it saw. */
async function triggerThroughPinPage(browser: Browser, slug: string, tags: MetricTags, role: string): Promise<TriggerVisit> {
    const context = await browser.newContext({ baseURL: env.baseUrl, storageState: storageStatePath(role), ignoreHTTPSErrors: env.ignoreHttpsErrors });
    const visit: TriggerVisit = { trigger: "private-pin-page", status: null, timings: null, boundaryStatus: null, error: "" };
    try {
        await installHtmxTracking(context);
        const page = await context.newPage();
        const boundaryPath = hrshRoutes.pinBoundary(slug);
        const boundaryResponse = page
            .waitForResponse((candidate) => new URL(candidate.url()).pathname === boundaryPath, { timeout: BOUNDARY_REQUEST_TIMEOUT_MS })
            .catch(() => null);
        const load = await openPrivatePin(page, slug, { metricPrefix: "hrsh.pin_page.setup_visit", tags });
        visit.status = load.status;
        visit.timings = load.timings;
        visit.boundaryStatus = (await boundaryResponse)?.status() ?? null;
        await waitForHtmxSettled(page, 30_000);
        await page.waitForTimeout(TRIGGER_DWELL_MS);
    } catch (error) {
        visit.error = (error as Error).message.split("\n")[0] ?? "unknown error";
    } finally {
        await context.close();
    }
    return visit;
}

function describeVisit(visit: TriggerVisit | null): string {
    if (visit === null) {
        return "The private pin page was never opened this run.";
    }
    const boundary = visit.boundaryStatus === null ? "never requested /boundary/" : `requested /boundary/ (HTTP ${visit.boundaryStatus})`;
    return `The private pin page answered HTTP ${visit.status ?? "nothing"} and ${boundary}${visit.error ? `; the visit failed: ${visit.error}` : ""}.`;
}

function noParcelDiagnosis(pin: CampusPin, visit: TriggerVisit | null): string {
    const area = pin.boundary ? Math.round(approximateAreaSqm(pin.boundary)) : 0;
    return (
        `No parcel geometry passed the size check within ${BOUNDARY_WAIT_MS / 60_000} minutes of opening the private pin page. The pin's boundary is ` +
        (pin.boundary
            ? `a ${pin.boundary.type} of about ${area.toLocaleString()} m², outside ${EXPECTED_PARCEL_AREA_SQM.min.toLocaleString()}-${EXPECTED_PARCEL_AREA_SQM.max.toLocaleString()} m² (REData measures the parcel at ${MEASURED_PARCEL_AREA_SQM.toLocaleString()} m²).`
            : "null.") +
        ` ${describeVisit(visit)} Enrichment was started only by that visit, never by the external API's panels/boundary/ endpoint. Check, in order: ` +
        "that the page requested /boundary/ (controllers/boundary.py schedules the chain from it); the account's external_apis_enabled; " +
        "UL_ALLOW_OUTBOUND_APIS and UL_REDATA_API_URL on the deployment; a Celery worker on the default queue; Location.place_id."
    );
}

/** Polls the pin (a read, which triggers nothing) until its parcel arrives or the wait runs out. */
async function waitForParcel(api: ApiClient, slug: string): Promise<CampusPin | null> {
    return waitForOrNull(() => readPin(api, slug), (value) => isRealParcel(value.boundary), {
        what: `the parcel boundary for pin ${slug}`,
        timeoutMs: BOUNDARY_WAIT_MS,
        intervalMs: BOUNDARY_POLL_INTERVAL_MS,
        describe: (value) => (value.boundary ? `a ${value.boundary.type} of about ${Math.round(approximateAreaSqm(value.boundary)).toLocaleString()} m²` : "boundary: null"),
    });
}

function fixtureFrom(site: SiteConfig, api: ApiClient, pin: CampusPin, state: CampusRunState, boundary: GeoJsonGeometry | null, diagnosis: string): CampusFixture {
    const origin: Coordinate = { label: `${site.key} pin`, latitude: pin.latitude, longitude: pin.longitude };
    return {
        site,
        api,
        pin,
        origin,
        nameAtSetup: state.nameAtSetup,
        nameIsPrivate: !HRSH_NAME_PATTERN.test(state.nameAtSetup),
        created: state.created,
        visit: state.visit,
        boundary,
        diagnosis,
        log: state.log.join("\n"),
        metresFromFirstPin: (point: Coordinate) => metresBetween(origin, point),
        requireBoundary: () => {
            suiteTest.skip(
                boundary === null,
                "no parcel geometry was provisioned for the campus, so this cannot be assessed. See the failure in hrsh-boundary.spec.ts, which reports that as the finding it is.",
            );
        },
    };
}

/** A later worker in the same run: reuse the first setup's pin, name, visit and verdict. Null when that pin has gone. */
async function resumeCampus(site: SiteConfig, api: ApiClient, state: CampusRunState): Promise<CampusFixture | null> {
    const response = await api.get(`pins/${state.pinSlug}/`);
    if (response.status() === 404) {
        return null;
    }
    if (!response.ok()) {
        throw new ApiError("GET", `pins/${state.pinSlug}/`, response.status(), await response.text());
    }
    let pin = (await response.json()) as CampusPin;
    const note = (line: string) => state.log.push(`[${new Date().toISOString()}] ${line}`);
    note(`worker ${process.pid} resumed the campus pin set up earlier in this run`);

    if (isRealParcel(pin.boundary)) {
        return fixtureFrom(site, api, pin, state, pin.boundary, "");
    }
    if (state.verdict) {
        return fixtureFrom(site, api, pin, state, null, state.verdict.diagnosis);
    }
    note("no verdict recorded yet (the first worker stopped mid-wait); waiting again");
    const settled = await waitForParcel(api, pin.slug);
    pin = settled ?? pin;
    const diagnosis = settled ? "" : noParcelDiagnosis(pin, state.visit);
    siteState(site).write({ ...state, verdict: { settled: settled !== null, diagnosis } });
    return fixtureFrom(site, api, pin, state, settled?.boundary ?? null, diagnosis);
}

/** Creates or adopts the site's pin, opens its private page, and waits for the parcel. Throws only when no pin can be had. */
async function buildSitePin(site: SiteConfig, request: APIRequestContext, browser: Browser): Promise<CampusFixture> {
    const api = new ApiClient(request, requireAccount(site.role).apiKey);
    const store = siteState(site);
    const point = site.point;

    const earlier = store.read();
    if (earlier) {
        const resumed = await resumeCampus(site, api, earlier);
        if (resumed) {
            return resumed;
        }
    }

    const log: string[] = [];
    const note = (line: string) => log.push(`[${new Date().toISOString()}] ${line}`);

    if (env.hrshFresh && earlier === null) {
        note("UL_E2E_HRSH_FRESH=1: deleting the campus pin and its children before creating a new one");
        await deleteCampusPins(api, note);
    }

    let created = false;
    let pin = await findExistingCampusPin(api, point);
    if (pin) {
        note(`adopted an existing campus pin: ${pin.slug} ("${pin.name}") at ${pin.latitude}, ${pin.longitude}`);
    } else {
        note(`creating a ${site.key} pin at ${point.latitude}, ${point.longitude}`);
        const response = await api.post("pins/", {
            name: site.privateName,
            latitude: point.latitude,
            longitude: point.longitude,
            description: `Created by the UrbanLens integration suite (run ${env.runId}).`,
            name_is_user_provided: true,
        });
        if (response.ok()) {
            pin = await readPin(api, ((await response.json()) as { slug: string }).slug);
            created = true;
            note(`created ${pin.slug}`);
        } else {
            // The app says the coordinate is taken, so whichever pin holds it is the campus pin.
            const refusal = (await response.text()).slice(0, 200);
            note(`create refused (${response.status()}): ${refusal} - re-searching`);
            pin = await findExistingCampusPin(api, point);
            if (pin === null) {
                throw new Error(
                    `Could not create a pin at ${point.latitude}, ${point.longitude} (${refusal}) and found no campus pin blocking it. ` +
                        "List the account's pins and remove whatever holds these coordinates, or run provision_integration_env --purge.",
                );
            }
            note(`adopted ${pin.slug} after the refusal`);
        }
    }

    const tags: MetricTags = { created, fresh: env.hrshFresh, site: site.key };
    const triggerStartedAt = Date.now();
    note(`trigger: opening the private pin page ${pinDetail(pin.slug)} signed in as ${site.role} (the external API panels/boundary/ endpoint is not called)`);
    const visit = await triggerThroughPinPage(browser, pin.slug, tags, site.role);
    note(describeVisit(visit));

    const state: CampusRunState = { pinSlug: pin.slug, created, nameAtSetup: pin.name, visit, verdict: null, log };
    store.write(state);

    let boundary: GeoJsonGeometry | null = null;
    let diagnosis = "";
    if (isRealParcel(pin.boundary)) {
        boundary = pin.boundary;
        note("parcel geometry was already present");
    } else {
        note("waiting for parcel geometry...");
        const settled = await waitForParcel(api, pin.slug);
        if (settled) {
            pin = settled;
            boundary = settled.boundary;
            recordMetric({ name: "hrsh.boundary.seconds_to_parcel", value: Math.round((Date.now() - triggerStartedAt) / 1000), unit: "s", tags });
            note("parcel geometry arrived");
        } else {
            pin = await readPin(api, pin.slug);
            diagnosis = noParcelDiagnosis(pin, visit);
            note(diagnosis);
        }
    }
    recordMetric({ name: "hrsh.boundary.parcel_arrived", value: boundary ? 1 : 0, unit: "count", tags });
    if (boundary) {
        recordMetric({ name: "hrsh.boundary.area_sqm", value: Math.round(approximateAreaSqm(boundary)), unit: "sqm", tags });
    }

    state.verdict = { settled: boundary !== null, diagnosis };
    store.write(state);
    return fixtureFrom(site, api, pin, state, boundary, diagnosis);
}

/** A worker-scoped fixture for one site's pin. Not deleted afterwards: the next run adopts it, and --purge is the cleanup. */
function siteFixture(site: SiteConfig): [WorkerFixture<CampusFixture, PlaywrightWorkerArgs>, { scope: "worker"; timeout: number }] {
    return [
        async ({ playwright, browser }, use) => {
            const request = await playwright.request.newContext({
                baseURL: env.baseUrl,
                ignoreHTTPSErrors: env.ignoreHttpsErrors,
                extraHTTPHeaders: { Accept: "application/json", "User-Agent": `UrbanLens-Integration-Tests/${env.runId}` },
            });
            try {
                await use(await buildSitePin(site, request, browser));
            } finally {
                await request.dispose();
            }
        },
        { scope: "worker", timeout: BOUNDARY_WAIT_MS + 5 * 60_000 },
    ];
}

/**
 * `test` for this directory, with the worker-scoped `campus` fixture (the requirement pin, primary account) and
 * `courtyard` (Jess's staging pin, secondary account). Opt-in via UL_E2E_LOCATION_DATA.
 */
export const locationDataTest = suiteTest.extend<{}, { campus: CampusFixture; courtyard: CampusFixture }>({
    campus: siteFixture(CAMPUS_SITE),
    courtyard: siteFixture(COURTYARD_SITE),
});

/** Child pins of the campus pin. There is no `pins/{slug}/children/`; `GET pins/` carries `parent_uuid`. */
export async function childPins(campus: CampusFixture): Promise<SyncPinRow[]> {
    return (await allPins(campus.api)).filter((row) => row.parent_uuid === campus.pin.uuid);
}

/**
 * Waits for at least `min` child pins and returns what was last seen, possibly fewer. Records `hrsh.child_pins.count`.
 *
 * A wait that already ran out this run is not repeated; the list is read once instead.
 */
export async function waitForChildPins(campus: CampusFixture, options: { min?: number; timeoutMs?: number; intervalMs?: number } = {}): Promise<SyncPinRow[]> {
    const min = options.min ?? 1;
    const key = `child-pins:${campus.pin.uuid}:${min}`;
    const startedAt = Date.now();
    let children = await childPins(campus);
    if (children.length < min && !isExhausted(key)) {
        const settled = await waitForOrNull(() => childPins(campus), (list) => list.length >= min, {
            what: `at least ${min} child pin(s) under the campus pin`,
            timeoutMs: options.timeoutMs ?? DEFAULT_CHILD_PIN_WAIT_MS,
            intervalMs: options.intervalMs ?? 15_000,
            describe: (list) => `${list.length} child pin(s)`,
        });
        if (settled) {
            children = settled;
            recordMetric({ name: "hrsh.child_pins.seconds_to_min", value: Math.round((Date.now() - startedAt) / 1000), unit: "s", tags: { min } });
        } else {
            children = await childPins(campus);
            markExhausted(key);
        }
    }
    recordMetric({ name: "hrsh.child_pins.count", value: children.length, unit: "count" });
    return children;
}

/**
 * Waits for `GET wikis/<locationSlug>/` to answer 200 and returns the payload, or null when it never does.
 *
 * Wikis are created automatically, so this only waits. A 5xx is thrown rather than waited out. A wait that already ran
 * out this run is not repeated. `metric` names an `s` metric for the time it took.
 */
export async function waitForWiki(api: ApiClient, locationSlug: string, options: { timeoutMs?: number; intervalMs?: number; metric?: string } = {}): Promise<WikiDetail | null> {
    const path = `wikis/${locationSlug}/`;
    const key = `wiki:${locationSlug}`;
    const startedAt = Date.now();
    const probe = async () => {
        const response = await api.get(path);
        if (response.status() >= 500) {
            throw new ApiError("GET", path, response.status(), await response.text());
        }
        return response;
    };
    const first = await probe();
    let response = first.status() === 200 || isExhausted(key) ? first : null;
    if (response === null) {
        try {
            response = await waitFor(probe, (candidate) => candidate.status() === 200, {
                what: `the wiki at ${path}`,
                timeoutMs: options.timeoutMs ?? DEFAULT_WIKI_WAIT_MS,
                intervalMs: options.intervalMs ?? 10_000,
                describe: (candidate) => `HTTP ${candidate.status()}`,
            });
            if (options.metric) {
                recordMetric({ name: options.metric, value: Math.round((Date.now() - startedAt) / 1000), unit: "s" });
            }
        } catch (error) {
            if (!(error instanceof WaitTimeoutError)) {
                throw error;
            }
            markExhausted(key);
            return null;
        }
    }
    return response.status() === 200 ? ((await response.json()) as WikiDetail) : null;
}

/** {@link waitForWiki} for the campus pin's own Location, recording `hrsh.wiki.available` and `hrsh.wiki.seconds_to_available`. */
export async function waitForCampusWiki(campus: CampusFixture, options: { timeoutMs?: number } = {}): Promise<WikiDetail | null> {
    const wiki = await waitForWiki(campus.api, campus.pin.location_slug, { ...options, metric: "hrsh.wiki.seconds_to_available" });
    recordMetric({ name: "hrsh.wiki.available", value: wiki ? 1 : 0, unit: "count" });
    return wiki;
}

/** Whether the campus wiki exists (waiting for it as {@link waitForCampusWiki} does). */
export async function ensureCampusWiki(campus: CampusFixture): Promise<boolean> {
    return (await waitForCampusWiki(campus)) !== null;
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
