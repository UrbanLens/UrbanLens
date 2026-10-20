/**
 * Fetches a keyless source in the viewer's own browser when the server says its upstream could not answer for it.
 *
 * A browser fetch costs the server the data, so the server asks for one only when its upstream could not answer. What is
 * fetched is posted back, where it is validated and kept as user-provided, less trusted than a direct query.
 */

import { sendJson } from "./fetch-json";
import type { FetchInit } from "./site-runtime";

const WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php";
const SEARCH_RADIUS_METERS = 1000;
const MAX_RESULTS = 20;

export interface WikipediaArticle {
    title: string;
    pageid: number;
    lat: number;
    lon: number;
}

interface GeosearchResponse {
    query?: { geosearch?: Array<Partial<WikipediaArticle>> };
}

/** Pages already tried on this page load, so a failed post is not retried each time the panel re-renders. */
const attempted = new Set<string>();

/** The Wikipedia articles placed within a kilometre of a point, as Wikipedia's own geosearch lists them. */
export async function fetchWikipediaNear(latitude: string, longitude: string): Promise<WikipediaArticle[]> {
    const params = new URLSearchParams({
        action: "query",
        list: "geosearch",
        gscoord: `${latitude}|${longitude}`,
        gsradius: String(SEARCH_RADIUS_METERS),
        gslimit: String(MAX_RESULTS),
        format: "json",
        formatversion: "2",
        origin: "*",
    });
    const init: FetchInit = { headers: { Accept: "application/json" }, __ulReported: true };
    const response = await fetch(`${WIKIPEDIA_API}?${params.toString()}`, init);
    if (!response.ok) return [];
    const body = (await response.json()) as GeosearchResponse;
    const found: WikipediaArticle[] = [];
    for (const raw of body.query?.geosearch ?? []) {
        if (typeof raw.title === "string" && Number.isInteger(raw.pageid) && typeof raw.lat === "number" && typeof raw.lon === "number") {
            found.push({ title: raw.title, pageid: raw.pageid as number, lat: raw.lat, lon: raw.lon });
        }
        if (found.length >= MAX_RESULTS) break;
    }
    return found;
}

async function run(el: HTMLElement): Promise<void> {
    const { latitude, longitude, postUrl, refreshUrl } = el.dataset;
    if (el.dataset.browserFetch !== "wikipedia" || !latitude || !longitude || !postUrl) return;
    const key = `${postUrl}|${latitude}|${longitude}`;
    if (attempted.has(key)) return;
    attempted.add(key);
    try {
        const results = await fetchWikipediaNear(latitude, longitude);
        if (!results.length) return;
        await sendJson(postUrl, "POST", { provider: "wikipedia", results }, { reportsItsOwnErrors: true });
        const target = el.closest<HTMLElement>(".article-sources");
        if (refreshUrl && target && document.body.contains(target)) {
            await window.htmx?.ajax("GET", refreshUrl, { target, swap: "innerHTML" });
        }
    } catch {
        // The panel already lists what the server already answered; a viewer whose browser cannot reach Wikipedia loses nothing.
    }
}

/** Fetch for every panel that asks, now and each time the Sources panel is swapped in. */
export function installBrowserFetch(): void {
    const scan = (): void => {
        document.querySelectorAll<HTMLElement>("[data-browser-fetch]").forEach((el) => void run(el));
    };
    document.body.addEventListener("htmx:afterSwap", scan);
    scan();
}

/** Forget what was tried; for tests. */
export function resetBrowserFetch(): void {
    attempted.clear();
}
