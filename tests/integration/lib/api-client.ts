/**
 * A thin, typed client for the versioned external API.
 *
 * Two jobs. The first is to make API-level assertions readable: every helper
 * returns the raw {@link APIResponse} so a spec can assert on status, headers
 * and body without the client deciding what counts as success.
 *
 * The second is to be the *setup* mechanism for UI specs. Driving the map form
 * to get a pin on screen tests pin creation, not the thing under test, and it
 * costs seconds per test. Creating the row over the API and then asserting on
 * the rendered page is both faster and a sharper failure signal - when the
 * assertion fails it is because rendering is broken, not because a toolbar
 * moved. Everything created this way is tracked and torn down by
 * {@link ApiClient.cleanup}, which the `api` fixture calls automatically.
 */

import type { APIRequestContext, APIResponse } from "@playwright/test";

import { apiUrl, env, resourceName } from "./env.js";

/** A resource this run created, and how to remove it again. */
interface TrackedResource {
    kind: string;
    identifier: string;
    remove: () => Promise<APIResponse>;
}

/** The subset of a created pin that specs and cleanup actually need. */
export interface CreatedPin {
    uuid: string;
    slug: string;
    name: string;
    latitude: number;
    longitude: number;
}

export interface PinOptions {
    name?: string;
    latitude?: number;
    longitude?: number;
    description?: string;
}

/**
 * Where a pin goes when a spec does not care.
 *
 * A real, unremarkable point rather than 0,0: the app resolves boundaries and
 * asks REData about whatever a pin claims to be, and Null Island produces empty
 * answers everywhere, which reads as a broken page rather than a plain one.
 */
const DEFAULT_LATITUDE = 42.6526;
const DEFAULT_LONGITUDE = -73.7562;

/**
 * Half-width of the box every default pin is scattered across, in degrees.
 *
 * Randomised rather than stepped, and this wide, because
 * `create_pin_for_profile` refuses a second pin close to an existing one
 * ("You already have a pin at this location") - and the pins it is comparing
 * against are everything *previous tests in this run* left behind, not just the
 * ones this client made. A per-client counter is not enough: every test's first
 * pin would land on the same coordinates and every test but the first would
 * fail. ~0.4 degrees is tens of kilometres, so a collision needs two draws to
 * land within metres of each other.
 */
const COORDINATE_SPREAD_DEGREES = 0.4;

/** Message `create_pin_for_profile` refuses a too-close pin with. */
const TOO_CLOSE_MESSAGE = "already have a pin at this location";

/** Attempts to find free coordinates before giving up. */
const PLACEMENT_ATTEMPTS = 4;

function scatter(base: number): number {
    return base + (Math.random() * 2 - 1) * COORDINATE_SPREAD_DEGREES;
}

export class ApiError extends Error {
    constructor(
        readonly method: string,
        readonly path: string,
        readonly status: number,
        readonly body: string,
    ) {
        super(`${method} ${path} -> ${status}\n${body.slice(0, 2000)}`);
        this.name = "ApiError";
    }
}

export class ApiClient {
    private readonly tracked: TrackedResource[] = [];
    private pinsCreated = 0;

    /**
     * @param request A Playwright request context. It does not need the bearer
     *     header pre-set; every call here adds it explicitly, so one context
     *     can be reused for anonymous checks too.
     * @param apiKey Raw `ulk_...` key, or null for an anonymous client.
     */
    constructor(
        private readonly request: APIRequestContext,
        readonly apiKey: string | null,
    ) {}

    /** Headers for an authenticated call; empty when this client is anonymous. */
    private authHeaders(): Record<string, string> {
        return this.apiKey ? { Authorization: `Bearer ${this.apiKey}` } : {};
    }

    async get(path: string, params?: Record<string, string | number | boolean>): Promise<APIResponse> {
        return this.request.get(apiUrl(path), { headers: this.authHeaders(), params });
    }

    async post(path: string, data?: unknown): Promise<APIResponse> {
        return this.request.post(apiUrl(path), { headers: this.authHeaders(), data: data ?? {} });
    }

    async patch(path: string, data?: unknown): Promise<APIResponse> {
        return this.request.patch(apiUrl(path), { headers: this.authHeaders(), data: data ?? {} });
    }

    async put(path: string, data?: unknown): Promise<APIResponse> {
        return this.request.put(apiUrl(path), { headers: this.authHeaders(), data: data ?? {} });
    }

    async delete(path: string): Promise<APIResponse> {
        return this.request.delete(apiUrl(path), { headers: this.authHeaders() });
    }

    /**
     * Runs a call and returns its parsed body, raising on a non-2xx.
     *
     * Use this for setup, where a failure means the test never got started.
     * Assertions about status codes belong on the raw helpers above.
     */
    async json<T = unknown>(method: "get" | "post" | "patch" | "put" | "delete", path: string, data?: unknown): Promise<T> {
        const response = method === "get" ? await this.get(path, data as Record<string, string> | undefined) : await this[method](path, data);
        if (!response.ok()) {
            throw new ApiError(method.toUpperCase(), path, response.status(), await response.text());
        }
        return (await response.json()) as T;
    }

    // -- Pins ---------------------------------------------------------------

    /**
     * Creates a pin owned by this client's account and schedules its deletion.
     *
     * @param options Overrides. Omitted coordinates get a jittered default so
     *     two pins created in one test are not merged into one another.
     */
    async createPin(options: PinOptions = {}): Promise<CreatedPin> {
        this.pinsCreated += 1;
        const name = options.name ?? resourceName(`pin ${this.pinsCreated}`);
        const explicitPlacement = options.latitude !== undefined || options.longitude !== undefined;

        let lastRefusal = "";
        for (let attempt = 0; attempt < PLACEMENT_ATTEMPTS; attempt += 1) {
            const latitude = options.latitude ?? scatter(DEFAULT_LATITUDE);
            const longitude = options.longitude ?? scatter(DEFAULT_LONGITUDE);

            const response = await this.post("pins/", {
                name,
                latitude,
                longitude,
                description: options.description ?? `Created by the UrbanLens integration suite (run ${env.runId}).`,
                // Without this the name is treated as machine-produced and may be
                // replaced by background enrichment, which would make an assertion
                // on the pin's own name intermittently wrong.
                name_is_user_provided: true,
            });

            if (!response.ok()) {
                const text = await response.text();
                // A caller who asked for specific coordinates gets the refusal:
                // moving their pin somewhere else would answer a different
                // question than the one they asked.
                if (explicitPlacement || !text.includes(TOO_CLOSE_MESSAGE)) {
                    throw new ApiError("POST", "pins/", response.status(), text);
                }
                lastRefusal = text;
                continue;
            }

            const body = (await response.json()) as { uuid: string; slug: string | null; name: string };
            if (!body.slug) {
                throw new ApiError("POST", "pins/", response.status(), `pin was created without a slug: ${JSON.stringify(body)}`);
            }

            const pin: CreatedPin = { uuid: body.uuid, slug: body.slug, name: body.name, latitude, longitude };
            this.track("pin", pin.slug, () => this.delete(`pins/${pin.slug}/`));
            return pin;
        }

        throw new ApiError("POST", "pins/", 400, `could not place a pin in ${PLACEMENT_ATTEMPTS} attempts; the account's existing pins cover the area. Last refusal: ${lastRefusal}`);
    }

    // -- Labels -------------------------------------------------------------

    /** Creates a label owned by this client's account and schedules its deletion. */
    async createLabel(name?: string): Promise<{ uuid: string; name: string }> {
        const label = await this.json<{ uuid: string; name: string }>("post", "labels/", {
            name: name ?? resourceName(`label ${Date.now()}`),
        });
        this.track("label", label.uuid, () => this.delete(`labels/${label.uuid}/`));
        return label;
    }

    // -- Cleanup ------------------------------------------------------------

    /**
     * Registers a resource for teardown.
     *
     * Public so a spec that creates something through an endpoint this client
     * has no helper for can still opt into automatic cleanup.
     */
    track(kind: string, identifier: string, remove: () => Promise<APIResponse>): void {
        this.tracked.push({ kind, identifier, remove });
    }

    /**
     * Deletes everything this client created, newest first.
     *
     * Never throws. A failed teardown must not turn a passing test red - the
     * account is disposable and `provision_integration_env --purge` is the
     * real backstop - but it is reported so a leak is visible rather than
     * silent.
     *
     * @returns Human-readable descriptions of resources that could not be removed.
     */
    async cleanup(): Promise<string[]> {
        const failures: string[] = [];
        for (const resource of [...this.tracked].reverse()) {
            try {
                const response = await resource.remove();
                // 404 is success: something else in the test already removed it.
                if (!response.ok() && response.status() !== 404) {
                    failures.push(`${resource.kind} ${resource.identifier}: HTTP ${response.status()}`);
                }
            } catch (error) {
                failures.push(`${resource.kind} ${resource.identifier}: ${(error as Error).message}`);
            }
        }
        this.tracked.length = 0;
        return failures;
    }
}
