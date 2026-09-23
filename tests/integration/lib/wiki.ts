/**
 * Community wikis for specs that need one to exist. Wikis are created by a background task after the
 * first pin lands on a Location (`tasks.ensure_wiki_for_location`), so every helper here polls
 * rather than assuming the page is there.
 */

import { expect, type APIResponse, type Page } from "@playwright/test";

import type { ApiClient, CreatedPin, PinOptions } from "./api-client.js";
import { env } from "./env.js";
import { appRoutes } from "./routes.js";
import { waitFor } from "./waiting.js";

/** How long a new pin's wiki is given to appear before the setup is declared broken. */
export const WIKI_WAIT_MS = 120_000;

const WIKI_POLL_INTERVAL_MS = 3_000;

/** The subset of `GET wikis/{location_slug}/` the specs read. */
export interface WikiDetail {
    location_slug: string;
    wiki_slug: string | null;
    uuid: string;
    name: string;
    description: string | null;
    boundary: unknown;
    aliases: Array<{ id: number; name: string }>;
}

export interface WikiArticle {
    content: string;
    base_revision_id: number | null;
}

export interface PinnedWiki {
    pin: CreatedPin;
    locationSlug: string;
    wiki: WikiDetail;
}

export interface Coordinates {
    latitude: number;
    longitude: number;
}

/**
 * A fresh point in the open Atlantic, well off the continental shelf.
 *
 * No parcel or building provider has geometry out there, so the Location never resolves a Place:
 * wiki access falls back to the exact-location rule, and place-level grants or a neighbouring
 * account's parcel cannot muddy an access assertion. Also far from HRSH and from the suite's
 * default Albany scatter.
 */
export function placelessCoordinates(): Coordinates {
    return {
        latitude: Number((38.6 + Math.random() * 1.2).toFixed(6)),
        longitude: Number((-70.4 + Math.random() * 1.8).toFixed(6)),
    };
}

export async function locationSlugOf(api: ApiClient, pinSlug: string): Promise<string> {
    const detail = await api.json<{ location_slug?: string }>("get", `pins/${pinSlug}/`);
    expect(detail.location_slug, `pins/${pinSlug}/ carried no location_slug, so nothing can address its wiki`).toBeTruthy();
    return String(detail.location_slug);
}

/**
 * Polls `wikis/{slug}/` as `api` until it answers 200.
 *
 * A 404 is the only "not yet" answer; anything else is returned to the caller as a failure rather
 * than waited out, because a 403 or 500 while waiting has already answered the question.
 */
export async function waitForWiki(api: ApiClient, locationSlug: string, timeoutMs = WIKI_WAIT_MS): Promise<WikiDetail> {
    const last = await waitFor(
        async () => {
            const response = await api.get(`wikis/${locationSlug}/`);
            return { status: response.status(), body: await response.text() };
        },
        (seen) => {
            if (seen.status !== 200 && seen.status !== 404) {
                throw new Error(`wikis/${locationSlug}/ answered ${seen.status} while waiting for the background wiki: ${seen.body.slice(0, 300)}`);
            }
            return seen.status === 200;
        },
        {
            what: `the auto-created wiki at wikis/${locationSlug}/ (tasks.ensure_wiki_for_location, queued by the pin's post_save signal)`,
            timeoutMs,
            intervalMs: WIKI_POLL_INTERVAL_MS,
            describe: (seen) => `HTTP ${seen.status}`,
        },
    );
    return JSON.parse(last.body) as WikiDetail;
}

/** Creates a pin as `api`, at fresh placeless coordinates unless told otherwise, and waits for its wiki. */
export async function pinWithWiki(api: ApiClient, options: PinOptions = {}): Promise<PinnedWiki> {
    const coordinates = options.latitude !== undefined && options.longitude !== undefined ? { latitude: options.latitude, longitude: options.longitude } : placelessCoordinates();
    const pin = await api.createPin({ ...options, ...coordinates });
    const locationSlug = await locationSlugOf(api, pin.slug);
    const wiki = await waitForWiki(api, locationSlug);
    return { pin, locationSlug, wiki };
}

/** The wiki's article, or null when none has been written yet (the route 404s for its own members then). */
export async function readArticle(api: ApiClient, locationSlug: string): Promise<WikiArticle | null> {
    const response = await api.get(`wikis/${locationSlug}/article/`);
    if (response.status() === 404) {
        return null;
    }
    expect(response.status(), `reading wikis/${locationSlug}/article/ answered ${response.status()}: ${(await response.text()).slice(0, 200)}`).toBe(200);
    return (await response.json()) as WikiArticle;
}

/** Saves `content` as the next article revision, quoting whatever revision is current. */
export async function writeArticle(api: ApiClient, locationSlug: string, content: string, editSummary = "integration suite"): Promise<APIResponse> {
    const current = await readArticle(api, locationSlug);
    return api.put(`wikis/${locationSlug}/article/`, {
        content,
        base_revision_id: current?.base_revision_id ?? null,
        edit_summary: editSummary,
    });
}

function stripSlugs(text: string, slugs: string[]): string {
    return slugs.reduce((acc, slug) => acc.split(slug).join("[slug]"), text);
}

/** The visible text of a styled 404 page's message block, with the requested slugs masked out. */
export function errorPageText(html: string, slugs: string[]): string {
    const pick = (pattern: RegExp) => (pattern.exec(html)?.[1] ?? "").replace(/<[^>]+>/g, " ");
    const block = [
        pick(/<title>([\s\S]*?)<\/title>/i),
        pick(/class="error-page__code"[^>]*>([\s\S]*?)<\/div>/),
        pick(/class="error-page__title"[^>]*>([\s\S]*?)<\/h1>/),
        pick(/class="error-page__body"[^>]*>([\s\S]*?)<\/p>/),
        // Django's DEBUG 404 page, which a dev stack serves instead of the styled one.
        pick(/<pre class="exception_value">([\s\S]*?)<\/pre>/),
        pick(/Raised by:<\/th>\s*<td>([\s\S]*?)<\/td>/),
    ].join(" | ");
    return stripSlugs(block, slugs).replace(/\s+/g, " ").trim();
}

/**
 * The HTML counterpart of `expectIndistinguishableFromMissing`.
 *
 * Whole bodies carry per-request nonces and tokens, so this compares the 404 page's message block
 * instead, after masking the slug each request echoed from its own URL.
 */
export async function expectPageIndistinguishableFromMissing(foreign: APIResponse, missing: APIResponse, slugs: string[], what: string): Promise<void> {
    expect(foreign.status(), `${what} answered ${foreign.status()} rather than looking absent`).toBe(404);
    expect(missing.status(), `the missing-slug control for ${what} answered ${missing.status()} rather than 404`).toBe(404);
    const foreignText = errorPageText(await foreign.text(), slugs);
    expect(foreignText, `${what}: the 404 page has no recognisable message block, so the comparison below would be empty against empty`).toMatch(/404|Page not found/);
    expect(foreignText, `${what}: the page for a real wiki you have not earned differs from the page for one that never existed`).toBe(errorPageText(await missing.text(), slugs));
}

/**
 * Headers that let a signed-in page's request context pass Django's CSRF check.
 *
 * Fetches a signed-in page through the context's request client when no `csrftoken` is held yet;
 * that shares the browser's cookie jar without a navigation the page's console guard would watch.
 * Referer is sent because Django's CSRF middleware insists on a same-origin one for HTTPS requests.
 */
export async function csrfHeaders(page: Page): Promise<Record<string, string>> {
    const find = async () => (await page.context().cookies()).find((cookie) => cookie.name === "csrftoken")?.value;
    let token = await find();
    if (!token) {
        await page.request.get(appRoutes.home);
        token = await find();
    }
    expect(token, "no csrftoken cookie after loading a signed-in page, so every session POST would fail CSRF instead of reaching the view").toBeTruthy();
    return { "X-CSRFToken": token ?? "", Referer: `${env.baseUrl}${appRoutes.home}` };
}
