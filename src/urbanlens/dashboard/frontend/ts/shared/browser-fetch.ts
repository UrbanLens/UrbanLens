/**
 * Fetches a keyless source in the viewer's own browser when the server says its upstream could not answer for it.
 *
 * A browser fetch costs the server the data, so the server asks for one only when its upstream could not answer or its
 * budget is nearly spent. What is fetched is posted back, where it is validated and kept as user-provided, less trusted
 * than a direct query. Wikipedia and Wikimedia Commons are asked for what lies near the pin; Overpass for the buildings
 * inside the parcel the server names. Each host is admitted by the page's CSP `connect-src` and nothing else is fetched.
 */

import { sendJson } from "./fetch-json";
import type { FetchInit } from "./site-runtime";

const WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php";
const COMMONS_API = "https://commons.wikimedia.org/w/api.php";
const OVERPASS_API = "https://overpass-api.de/api/interpreter";
const SEARCH_RADIUS_METERS = 1000;
const MAX_RESULTS = 20;
/** The Commons namespace of files. */
const COMMONS_FILE_NAMESPACE = "6";
/** The file namespace also holds PDFs, audio and video, so more are asked for than are kept. */
const COMMONS_SEARCH_LIMIT = "50";
/** The file types the server builds a thumbnail for. */
export const COMMONS_IMAGE_EXTENSIONS = new Set(["jpg", "jpeg", "png", "gif", "webp", "svg"]);
/** The server accepts at most this many buildings and outline points per post. */
const MAX_BUILDINGS = 150;
const MAX_RING_POINTS = 200;
/** An Overpass answer larger than this is dropped unread: a parcel's buildings are a few hundred kilobytes at most. */
export const MAX_OVERPASS_BYTES = 2 * 1024 * 1024;

export interface WikipediaArticle {
    title: string;
    pageid: number;
    lat: number;
    lon: number;
}

/** A Commons file placed near the pin; the same fields as an article, whose title is `File:Name.ext`. */
export type CommonsFile = WikipediaArticle;

export interface OsmBuilding {
    type: "way" | "relation";
    id: number;
    lat: number;
    lon: number;
    name?: string;
    ref?: string;
    /** `[lon, lat]` pairs of a closed outline; only a way has one. */
    ring?: Array<[number, number]>;
}

interface GeosearchResponse {
    query?: { geosearch?: Array<Partial<WikipediaArticle>> };
}

interface OverpassElement {
    type?: string;
    id?: number;
    bounds?: { minlat?: number; minlon?: number; maxlat?: number; maxlon?: number };
    geometry?: Array<{ lat?: number; lon?: number }>;
    tags?: Record<string, unknown>;
}

/** Pages already tried on this page load, so a failed post is not retried each time the panel re-renders. */
const attempted = new Set<string>();

const JSON_INIT: FetchInit = { headers: { Accept: "application/json" }, __ulReported: true };

/** The pages a MediaWiki site's geosearch places within a kilometre of a point. */
async function geosearch(api: string, latitude: string, longitude: string, extra: Record<string, string> = {}, keep: (title: string) => boolean = () => true): Promise<WikipediaArticle[]> {
    const params = new URLSearchParams({
        action: "query",
        list: "geosearch",
        gscoord: `${latitude}|${longitude}`,
        gsradius: String(SEARCH_RADIUS_METERS),
        gslimit: String(MAX_RESULTS),
        format: "json",
        formatversion: "2",
        origin: "*",
        ...extra,
    });
    const response = await fetch(`${api}?${params.toString()}`, JSON_INIT);
    if (!response.ok) return [];
    const body = (await response.json()) as GeosearchResponse;
    const found: WikipediaArticle[] = [];
    for (const raw of body.query?.geosearch ?? []) {
        if (typeof raw.title === "string" && keep(raw.title) && Number.isInteger(raw.pageid) && typeof raw.lat === "number" && typeof raw.lon === "number") {
            found.push({ title: raw.title, pageid: raw.pageid as number, lat: raw.lat, lon: raw.lon });
        }
        if (found.length >= MAX_RESULTS) break;
    }
    return found;
}

/** The Wikipedia articles placed within a kilometre of a point, as Wikipedia's own geosearch lists them. */
export function fetchWikipediaNear(latitude: string, longitude: string): Promise<WikipediaArticle[]> {
    return geosearch(WIKIPEDIA_API, latitude, longitude);
}

/** Whether a Commons file title names an image. */
export function isCommonsImage(title: string): boolean {
    const dot = title.lastIndexOf(".");
    return dot > 0 && COMMONS_IMAGE_EXTENSIONS.has(title.slice(dot + 1).toLowerCase());
}

/** The Commons files placed within a kilometre of a point, as Commons' own geosearch lists them. */
export function fetchCommonsNear(latitude: string, longitude: string): Promise<CommonsFile[]> {
    return geosearch(COMMONS_API, latitude, longitude, { gsnamespace: COMMONS_FILE_NAMESPACE, gslimit: COMMONS_SEARCH_LIMIT }, isCommonsImage);
}

/** The body of a response, or null when it is longer than `limit` bytes (checked as it arrives, not trusted from a header). */
export async function readCapped(response: Response, limit: number): Promise<string | null> {
    const declared = Number(response.headers.get("content-length"));
    if (Number.isFinite(declared) && declared > limit) return null;
    const reader = response.body?.getReader();
    if (!reader) {
        const text = await response.text();
        return text.length > limit ? null : text;
    }
    const decoder = new TextDecoder();
    let received = 0;
    let text = "";
    for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        received += value.byteLength;
        if (received > limit) {
            await reader.cancel();
            return null;
        }
        text += decoder.decode(value, { stream: true });
    }
    return text + decoder.decode();
}

function finite(value: unknown): value is number {
    return typeof value === "number" && Number.isFinite(value);
}

function outline(geometry: OverpassElement["geometry"]): Array<[number, number]> | undefined {
    if (!Array.isArray(geometry) || geometry.length < 4) return undefined;
    const points: Array<[number, number]> = [];
    for (const point of geometry) {
        if (!finite(point.lon) || !finite(point.lat)) return undefined;
        points.push([point.lon, point.lat]);
    }
    const first = points[0]!;
    const last = points[points.length - 1]!;
    if (first[0] !== last[0] || first[1] !== last[1]) return undefined;
    if (points.length <= MAX_RING_POINTS) return points;
    // Too many vertices for one post: every nth point, still closed on the first.
    const step = Math.ceil((points.length - 1) / (MAX_RING_POINTS - 1));
    const thinned = points.slice(0, -1).filter((_, index) => index % step === 0);
    return [...thinned, thinned[0]!];
}

/** The buildings in an Overpass answer that carry what the server needs: an identity and a centre. */
export function parseOverpassBuildings(text: string): OsmBuilding[] {
    let body: { elements?: OverpassElement[] };
    try {
        body = JSON.parse(text) as { elements?: OverpassElement[] };
    } catch {
        return [];
    }
    const found: OsmBuilding[] = [];
    for (const element of body.elements ?? []) {
        const bounds = element.bounds;
        if ((element.type !== "way" && element.type !== "relation") || !Number.isInteger(element.id) || !bounds) continue;
        if (!finite(bounds.minlat) || !finite(bounds.maxlat) || !finite(bounds.minlon) || !finite(bounds.maxlon)) continue;
        const building: OsmBuilding = {
            type: element.type,
            id: element.id as number,
            lat: (bounds.minlat + bounds.maxlat) / 2,
            lon: (bounds.minlon + bounds.maxlon) / 2,
        };
        const tags = element.tags ?? {};
        if (typeof tags.name === "string" && tags.name) building.name = tags.name;
        const ref = tags.ref ?? tags["addr:housenumber"];
        if (typeof ref === "string" && ref) building.ref = ref;
        const ring = element.type === "way" ? outline(element.geometry) : undefined;
        if (ring) building.ring = ring;
        found.push(building);
        if (found.length >= MAX_BUILDINGS) break;
    }
    return found;
}

/**
 * The OpenStreetMap buildings inside a parcel, from Overpass.
 *
 * @param polygon The parcel outline as the server gives it: Overpass's `poly:` text, `lat lon lat lon ...`.
 */
export async function fetchOverpassBuildings(polygon: string): Promise<OsmBuilding[]> {
    if (!/^[-0-9. ]+$/.test(polygon)) return [];
    const query = `[out:json][timeout:25];(way(poly:"${polygon}")["building"];relation(poly:"${polygon}")["type"="multipolygon"]["building"];);out tags geom;`;
    const init: FetchInit = {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ data: query }).toString(),
        __ulReported: true,
    };
    const response = await fetch(OVERPASS_API, init);
    if (!response.ok) return [];
    const text = await readCapped(response, MAX_OVERPASS_BYTES);
    return text === null ? [] : parseOverpassBuildings(text);
}

async function fetchFor(el: HTMLElement, provider: string): Promise<unknown[]> {
    const { latitude, longitude, polygon } = el.dataset;
    if (provider === "wikipedia" && latitude && longitude) return fetchWikipediaNear(latitude, longitude);
    if (provider === "wikimedia_commons" && latitude && longitude) return fetchCommonsNear(latitude, longitude);
    if (provider === "overpass" && polygon) return fetchOverpassBuildings(polygon);
    return [];
}

async function run(el: HTMLElement): Promise<void> {
    const provider = el.dataset.browserFetch ?? "";
    const { postUrl, refreshUrl } = el.dataset;
    if (!provider || !postUrl) return;
    const key = `${postUrl}|${provider}|${el.dataset.latitude ?? ""}|${el.dataset.longitude ?? ""}|${el.dataset.polygon ?? ""}`;
    if (attempted.has(key)) return;
    attempted.add(key);
    try {
        const results = await fetchFor(el, provider);
        if (!results.length) return;
        await sendJson(postUrl, "POST", { provider, results }, { reportsItsOwnErrors: true });
        const target = el.dataset.refreshTarget ? document.querySelector<HTMLElement>(el.dataset.refreshTarget) : el.closest<HTMLElement>(".article-sources");
        const swap = (el.dataset.refreshSwap ?? "innerHTML") as "innerHTML" | "outerHTML" | "beforeend";
        if (refreshUrl && target && document.body.contains(target)) {
            await window.htmx?.ajax("GET", refreshUrl, { target, swap });
        }
    } catch {
        // The panel already lists what the server already answered; a viewer whose browser cannot reach the source loses nothing.
    }
}

/** Fetch for every panel that asks, now and each time a panel is swapped in. */
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
