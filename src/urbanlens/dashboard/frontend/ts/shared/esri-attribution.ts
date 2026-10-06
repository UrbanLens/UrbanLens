/**
 * Credits for Esri's basemaps that name the providers of the area on screen, not the whole world's.
 *
 * Esri's terms ask a map built on its basemaps for two things: "Powered by Esri", and the data
 * providers - shown "dynamically as zoom levels change", which is what its own esri-leaflet does.
 * Each basemap publishes who covers where, by bounding box and zoom range, at
 * `static.arcgis.com/attribution/<service>`; the same file esri-leaflet reads.
 *
 * Until that file arrives, or if it never does, a layer is credited with the static string its def
 * carries - the full list, which says more than the view needs and never less.
 */

/** Esri's own credit, required wherever one of its basemaps is drawn. */
export const POWERED_BY_ESRI = "Powered by Esri";

const ATTRIBUTION_ROOT = "https://static.arcgis.com/attribution/";

/** One provider's coverage of one area, flattened from the service's contributor list. */
export interface EsriCoverage {
    attribution: string;
    score: number;
    south: number;
    west: number;
    north: number;
    east: number;
    zoomMin: number;
    zoomMax: number;
}

/** The view a credit is for, in degrees. */
export interface AttributionView {
    south: number;
    west: number;
    north: number;
    east: number;
    zoom: number;
}

interface ContributorsDocument {
    contributors?: Array<{
        attribution?: unknown;
        coverageAreas?: Array<{ score?: unknown; bbox?: unknown; zoomMin?: unknown; zoomMax?: unknown }>;
    }>;
}

/** Matches an ArcGIS Online tile URL and captures the service path, folder included. */
const ESRI_TILE_URL = /^https:\/\/(?:server|services)\.arcgisonline\.com\/ArcGIS\/rest\/services\/(.+?)\/MapServer\//i;

/** Only these characters reach the attribution URL; a catalogue value outside them is not a service name. */
const SERVICE_NAME = /^[A-Za-z0-9_]+(?:\/[A-Za-z0-9_]+)*$/;

/**
 * The Esri service an XYZ template draws from, or null for any other vendor.
 * @param url - A tile URL template.
 */
export function esriServiceForUrl(url: string): string | null {
    return ESRI_TILE_URL.exec(url)?.[1] ?? null;
}

/** Whether `name` can safely be appended to the attribution root. */
export function isEsriServiceName(name: unknown): name is string {
    return typeof name === "string" && SERVICE_NAME.test(name);
}

function finite(value: unknown): number | null {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Flattens a contributors document into coverages, highest score first - the order esri-leaflet
 * lists them in, so the most specific source for an area leads.
 * @param doc - The parsed `static.arcgis.com/attribution/<service>` body.
 */
export function parseContributors(doc: unknown): EsriCoverage[] {
    const coverages: EsriCoverage[] = [];
    const contributors = (doc as ContributorsDocument | null)?.contributors;
    if (!Array.isArray(contributors)) return coverages;
    for (const contributor of contributors) {
        const text = typeof contributor?.attribution === "string" ? contributor.attribution.trim() : "";
        if (!text || !Array.isArray(contributor.coverageAreas)) continue;
        for (const area of contributor.coverageAreas) {
            const bbox = Array.isArray(area?.bbox) ? area.bbox.map(finite) : [];
            const [south, west, north, east] = bbox;
            const zoomMin = finite(area?.zoomMin);
            const zoomMax = finite(area?.zoomMax);
            if (south == null || west == null || north == null || east == null || zoomMin == null || zoomMax == null) continue;
            coverages.push({ attribution: text, score: finite(area?.score) ?? 0, south, west, north, east, zoomMin, zoomMax });
        }
    }
    // Stable on equal scores, so the provider order the service gives is kept.
    return coverages.map((coverage, index) => ({ coverage, index })).sort((a, b) => b.coverage.score - a.coverage.score || a.index - b.index).map(({ coverage }) => coverage);
}

/** Wraps a longitude into [-180, 180]. */
function wrapLng(lng: number): number {
    const wrapped = ((((lng + 180) % 360) + 360) % 360) - 180;
    return wrapped === -180 && lng > 0 ? 180 : wrapped;
}

/** The view's longitude span as one or two ranges inside [-180, 180], split where it crosses the antimeridian. */
function lngRanges(west: number, east: number): Array<[number, number]> {
    if (east - west >= 360) return [[-180, 180]];
    const w = wrapLng(west);
    const e = wrapLng(east);
    return w <= e ? [[w, e]] : [[w, 180], [-180, e]];
}

/**
 * The providers to credit for a view, in the order esri-leaflet shows them, each named once.
 * @param coverages - From {@link parseContributors}.
 * @param view - The visible bounds and the zoom of the tiles drawn.
 */
export function contributorsFor(coverages: readonly EsriCoverage[], view: AttributionView): string[] {
    const names: string[] = [];
    const ranges = lngRanges(view.west, view.east);
    const zoom = Math.round(view.zoom);
    for (const coverage of coverages) {
        if (names.includes(coverage.attribution)) continue;
        if (zoom < coverage.zoomMin || zoom > coverage.zoomMax) continue;
        if (coverage.south > view.north || coverage.north < view.south) continue;
        if (!ranges.some(([w, e]) => coverage.west <= e && coverage.east >= w)) continue;
        names.push(coverage.attribution);
    }
    return names;
}

/** Each service's coverages once fetched - shared by every map on the page. */
const loaded = new Map<string, EsriCoverage[]>();
/** Requests in flight, so two maps asking at once make one request, with whoever is waiting on each. */
const pending = new Map<string, { request: Promise<EsriCoverage[] | null>; waiting: Set<() => void> }>();
/** When each service last failed to load. */
const failedAt = new Map<string, number>();

/**
 * How long a failed service is left alone before it is asked again. Every pan asks, and a request the
 * page's CSP refuses also files a violation report, so a deployment that has not admitted the host
 * must not send one per pan.
 */
export const RETRY_AFTER_FAILURE_MS = 60_000;

/**
 * The service's coverages if they have already arrived, else null - and the request for them is made,
 * so `onReady` runs once they have.
 * @param service - An Esri service name such as `World_Imagery`.
 * @param onReady - Called when coverages that were not yet here have arrived; once per request however
 *   often it is passed.
 */
export function esriCoverages(service: string, onReady?: () => void): EsriCoverage[] | null {
    if (!isEsriServiceName(service)) return null;
    const ready = loaded.get(service);
    if (ready) return ready;
    let entry = pending.get(service);
    if (!entry) {
        const failed = failedAt.get(service);
        if (failed !== undefined && Date.now() - failed < RETRY_AFTER_FAILURE_MS) return null;
        const waiting = new Set<() => void>();
        const request = fetchCoverages(service).then((coverages) => {
            pending.delete(service);
            if (coverages) {
                failedAt.delete(service);
                for (const callback of waiting) callback();
            } else {
                // Forgotten after a while rather than kept: an outage is not an answer.
                failedAt.set(service, Date.now());
            }
            return coverages;
        });
        entry = { request, waiting };
        pending.set(service, entry);
    }
    if (onReady) entry.waiting.add(onReady);
    return null;
}

/** What asks Esri; replaced in unit tests, which must not depend on a third party being up. */
let fetchAttribution: (url: string, init: RequestInit) => Promise<Response> = (url, init) => fetch(url, init);

/**
 * Replaces the request Esri's coverages are fetched with. Test-only: the unit-test preload points it
 * at a refusal, so no test reaches the network unless it says what Esri answers.
 */
export function setEsriAttributionFetchForTests(impl: (url: string, init: RequestInit) => Promise<Response>): void {
    fetchAttribution = impl;
}

async function fetchCoverages(service: string): Promise<EsriCoverage[] | null> {
    try {
        // No credentials and no referrer: the request names the basemap and nothing about the page.
        const response = await fetchAttribution(`${ATTRIBUTION_ROOT}${service}`, { credentials: "omit", referrerPolicy: "no-referrer" });
        if (!response.ok) return null;
        const coverages = parseContributors(await response.json());
        if (!coverages.length) return null;
        loaded.set(service, coverages);
        return coverages;
    } catch {
        return null;
    }
}

/** Resolves when any request for `service` now in flight has settled. Test-only. */
export function settledForTests(service: string): Promise<unknown> {
    return pending.get(service)?.request ?? Promise.resolve();
}

/** Forgets every fetched and in-flight service. Test-only. */
export function resetEsriAttributionForTests(): void {
    loaded.clear();
    pending.clear();
    failedAt.clear();
}

/** Seeds a service's coverages without a request. Test-only. */
export function seedEsriCoveragesForTests(service: string, coverages: EsriCoverage[]): void {
    loaded.set(service, coverages);
}

/** One drawn layer's credit, as an attribution line is built from them. */
export type CreditSource =
    | { kind: "text"; text: string }
    | {
          kind: "esri";
          service: string;
          /** The def's static credit, shown until the service's coverages are here. */
          fallback: string;
          /** Deepest zoom the tiles exist at; past it the map shows these tiles enlarged. */
          maxNativeZoom?: number;
      };

/**
 * One attribution line for the layers drawn: "Powered by Esri" once if any of them is Esri's, then
 * each layer's providers, then the renderer.
 * @param sources - One per drawn layer, base first.
 * @param view - The visible area, or null when it is not known yet.
 * @param engine - "Leaflet" or "MapLibre"; empty to leave it off (an exported image).
 * @param onReady - Called when a service's coverages arrive, so the caller can build the line again.
 */
export function composeAttribution(sources: readonly CreditSource[], view: AttributionView | null, engine: string, onReady?: () => void): string {
    const parts: string[] = [];
    let esri = false;
    for (const source of sources) {
        if (source.kind === "text") {
            if (source.text && !parts.includes(source.text)) parts.push(source.text);
            continue;
        }
        esri = true;
        const coverages = esriCoverages(source.service, onReady);
        let text = source.fallback;
        if (coverages && view) {
            const zoom = source.maxNativeZoom != null ? Math.min(view.zoom, source.maxNativeZoom) : view.zoom;
            const names = contributorsFor(coverages, { ...view, zoom });
            if (names.length) text = names.join(", ");
        }
        if (text && !parts.includes(text)) parts.push(text);
    }
    if (esri) parts.unshift(POWERED_BY_ESRI);
    if (engine) parts.push(engine);
    return parts.join(" · ");
}
