/**
 * The street and dark vector basemaps' tiles from Protomaps' hosted API, fetched by the browser directly, with
 * this deployment's own tiles as the automatic fallback.
 *
 * Only the tile source moves. The style, its glyphs and its sprites stay the ones REData publishes on our own
 * mirror, and that style's `protomaps` source keeps naming our own tiles (`tiles.urbanlens.org/basemap/{z}/{x}/{y}`).
 * Where `{% hosted_basemap_tiles %}` embedded a hosted template (production and staging), this module points that
 * one source at it, and points it back at the style's own tiles when the hosted ones fail. Both serve the
 * Protomaps v4 schema - the same layers and attributes, compared tile by tile on 2026-10-06 - so the style draws
 * either. Without the embed (development, local, no key) nothing here does anything.
 *
 * Failure means a tile answered 401 or 403 (a wrong key or origin), at once; otherwise {@link ERROR_THRESHOLD}
 * failed tiles within {@link ERROR_WINDOW_MS}, network failures included. They have to be: the API sends no CORS
 * header with a refusal (measured: a wrong key is a bare 403), so in a browser a refused tile surfaces as a network
 * error with no status at all. MapLibre swallows a 404 and an aborted request itself, so neither counts.
 *
 * On failure every map on the page swaps to our own tiles, one `console.warn` says so, and the choice is
 * remembered in `sessionStorage` for {@link REPROBE_AFTER_MS}: pages opened in that time start on our own tiles,
 * and the first one after it tries the hosted tiles again. A page never swaps back, so nothing can flap.
 */

import type { ErrorEvent, Map as MaplibreMap, SourceSpecification } from "maplibre-gl";

/** `<script type="application/json">` written by `{% hosted_basemap_tiles %}` in `themes/base.html`. */
export const HOSTED_BASEMAP_EMBED_ID = "ul-hosted-basemap";

/** The source our street and dark styles draw the Protomaps basemap from, as Protomaps' own style builder names it. */
export const BASEMAP_SOURCE_ID = "protomaps";

/** The layers drawn from that source. Terrain is a raster DEM and has none. */
const HOSTED_KINDS: ReadonlySet<string> = new Set(["street", "dark"]);

/** What Protomaps asks a map drawing its ZXY tiles to show. */
export const HOSTED_BASEMAP_ATTRIBUTION = '<a href="https://protomaps.com">Protomaps</a> &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';

/** When this tab last gave up on the hosted tiles, in epoch milliseconds. */
export const FALLBACK_STORAGE_KEY = "ul-hosted-basemap-fallback-at";

/** How long a fallback holds for new pages in the same tab before the hosted tiles are tried again. */
export const REPROBE_AFTER_MS = 30 * 60 * 1000;

/** Failed tiles that count as the hosted source failing, and the window they have to fall within. */
export const ERROR_THRESHOLD = 3;
export const ERROR_WINDOW_MS = 10_000;

/** How long after a swap a failing tile is taken to be a hosted request that was already in flight. */
export const LATE_FAILURE_WINDOW_MS = 30_000;

/** The hosted template this page draws from, `null` for the style's own tiles, `undefined` until first asked. */
let pageTemplate: string | null | undefined;
/** When recent hosted tiles failed, oldest first, across every map on the page. */
let failures: number[] = [];
/** One per map drawing the hosted tiles: points that map's source back at its own tiles. */
const swappers = new Set<() => void>();
/** Told once, when the page falls back - so a credit line naming Protomaps can stop. */
const fallbackListeners = new Set<() => void>();

function embeddedTemplate(): string | null {
    // Guarded rather than read at module scope: this module is imported by bundles that run before the DOM exists.
    if (typeof document === "undefined") return null;
    const text = document.getElementById(HOSTED_BASEMAP_EMBED_ID)?.textContent;
    if (!text) return null;
    try {
        const tiles = (JSON.parse(text) as { tiles?: unknown } | null)?.tiles;
        return typeof tiles === "string" && tiles.startsWith("https://") && ["{z}", "{x}", "{y}"].every((token) => tiles.includes(token)) ? tiles : null;
    } catch {
        return null;
    }
}

/** Whether this tab fell back recently enough that a new page should not try the hosted tiles yet. */
function fellBackRecently(now: number): boolean {
    try {
        const at = Number(window.sessionStorage.getItem(FALLBACK_STORAGE_KEY) ?? Number.NaN);
        // An unreadable entry is no reason to stay off the hosted tiles: a failure writes a fresh one.
        return Number.isFinite(at) && now - at < REPROBE_AFTER_MS;
    } catch {
        return false;
    }
}

function rememberFallback(now: number): void {
    try {
        window.sessionStorage.setItem(FALLBACK_STORAGE_KEY, String(now));
    } catch {
        /* storage unavailable - this page still falls back, the next one simply tries again */
    }
}

/**
 * The hosted tile template this page draws the street and dark basemaps from, or `null` for the style's own tiles.
 *
 * Decided once per page, so a credit line and the tiles under it cannot disagree because a remembered fallback
 * expired while the page was open.
 */
export function hostedTilesTemplate(): string | null {
    if (pageTemplate === undefined) {
        const template = embeddedTemplate();
        pageTemplate = template && !fellBackRecently(Date.now()) ? template : null;
    }
    return pageTemplate;
}

/**
 * The hosted template for one base layer key, or `null` when that layer draws its style's own tiles.
 * @param kind - The `VECTOR_STYLE_DEFS` key being drawn.
 */
export function hostedTilesFor(kind: string): string | null {
    return HOSTED_KINDS.has(kind) ? hostedTilesTemplate() : null;
}

/**
 * The credit for a vector base layer: Protomaps' while its hosted tiles are drawn, else the layer's own.
 * @param kind - The `VECTOR_STYLE_DEFS` key being drawn.
 * @param def - The style being drawn: its own attribution, and whether it is a public built-in (OpenFreeMap), whose
 * tiles the hosted ones never replace.
 */
export function vectorBaseCredit(kind: string, def: { attribution: string; builtIn?: boolean }): string {
    return !def.builtIn && hostedTilesFor(kind) ? HOSTED_BASEMAP_ATTRIBUTION : def.attribution;
}

/**
 * Calls `listener` when the page falls back to its own tiles.
 * @returns Unsubscribes.
 */
export function onHostedBasemapFallback(listener: () => void): () => void {
    fallbackListeners.add(listener);
    return () => fallbackListeners.delete(listener);
}

/** The style's own tile list for `source`, when it is the plain `tiles` shape this module can swap back to. */
function ownTilesOf(source: SourceSpecification | undefined): string[] | null {
    if (!source || source.type !== "vector") return null;
    // A TileJSON or `pmtiles://` source names its tiles somewhere else, so there is nothing to put back.
    if (source.url) return null;
    return Array.isArray(source.tiles) && source.tiles.length ? [...source.tiles] : null;
}

/**
 * Points one source of a style document at the hosted tiles, before MapLibre has seen it.
 *
 * For the MapLibre engine, which fetches and merges the style itself (`maplibre-layers.ts`).
 * @param sources - The style's sources, rewritten in place.
 * @param sourceId - The basemap source's id in `sources`.
 * @param kind - The base layer key the style draws.
 * @returns The tiles to fall back to, or `null` when nothing was changed.
 */
export function useHostedTiles(sources: Record<string, SourceSpecification>, sourceId: string, kind: string): string[] | null {
    const template = hostedTilesFor(kind);
    const own = ownTilesOf(sources[sourceId]);
    if (!template || !own) return null;
    sources[sourceId] = { ...sources[sourceId], tiles: [template] } as SourceSpecification;
    return own;
}

/** The HTTP status a MapLibre tile error carries, if it got that far. */
function statusOf(error: unknown): number | null {
    const status = (error as { status?: unknown } | null)?.status;
    return typeof status === "number" ? status : null;
}

function fallBack(reason: string): void {
    if (pageTemplate === null) return;
    pageTemplate = null;
    failures = [];
    rememberFallback(Date.now());
    console.warn(`Basemap: Protomaps' hosted tiles failed (${reason}); drawing this site's own tiles instead.`);
    for (const swap of [...swappers]) swap();
    for (const listener of [...fallbackListeners]) listener();
}

/** The tile an error is about, as `refreshTiles` takes it, when MapLibre said. */
function failedTile(event: unknown): { x: number; y: number; z: number } | null {
    const canonical = (event as { tile?: { tileID?: { canonical?: { x?: unknown; y?: unknown; z?: unknown } } } }).tile?.tileID?.canonical;
    const { x, y, z } = canonical ?? {};
    return typeof x === "number" && typeof y === "number" && typeof z === "number" ? { x, y, z } : null;
}

/**
 * Watches one map's hosted basemap source, and swaps it to `ownTiles` when the hosted tiles fail.
 *
 * Listening for `error` stops MapLibre printing every error itself, so the ones that are not this source's are
 * printed here instead, as it would have. This source's are counted rather than printed.
 * @param map - The MapLibre map drawing the source.
 * @param sourceId - The source's id on that map.
 * @param ownTiles - The tiles the style itself named.
 * @returns Stops watching; call it when the source or the map goes away.
 */
export function watchHostedTiles(map: MaplibreMap, sourceId: string, ownTiles: string[]): () => void {
    /** Tiles retried once after the swap, by `z/x/y`, so a tile of our own that keeps failing is not retried forever. */
    const retried = new Set<string>();
    /** Tiles whose hosted load failed before the swap, by `z/x/y`. */
    const failedBeforeSwap = new Map<string, { x: number; y: number; z: number }>();
    let swappedAt: number | null = null;
    const refresh = (tiles: Array<{ x: number; y: number; z: number }>): void => {
        if (tiles.length && typeof map.refreshTiles === "function") map.refreshTiles(sourceId, tiles);
    };
    const swap = (): void => {
        swappedAt = Date.now();
        const source = map.getSource(sourceId) as { setTiles?: (tiles: string[]) => unknown } | undefined;
        source?.setTiles?.(ownTiles);
        // `setTiles` re-points the template at once but reloads a frame later, and that reload leaves a tile that had
        // already failed waiting on a load that never comes (MapLibre 6.12, measured in a browser on 2026-10-06): the
        // tiles that failed before the swap stayed blank. Asked for again now, they come from our own tiles.
        for (const key of failedBeforeSwap.keys()) retried.add(key);
        refresh([...failedBeforeSwap.values()]);
        failedBeforeSwap.clear();
    };
    const onError = (event: ErrorEvent & { sourceId?: string }): void => {
        if (event.sourceId !== sourceId) {
            console.error(event.error);
            return;
        }
        if (pageTemplate === null) {
            // Already on our own tiles. A hosted request still in flight at the swap fails after it, and MapLibre does
            // not retry a tile whose load fails once its source has changed, so that tile would stay blank: it is asked
            // for once more, now from our own tiles. Anything after that is a real failure of our own tiles, printed as
            // MapLibre would have.
            const tile = failedTile(event);
            const key = tile ? `${tile.z}/${tile.x}/${tile.y}` : null;
            const late = swappedAt !== null && Date.now() - swappedAt < LATE_FAILURE_WINDOW_MS;
            if (tile && key && late && !retried.has(key) && typeof map.refreshTiles === "function") {
                retried.add(key);
                refresh([tile]);
                return;
            }
            console.error(event.error);
            return;
        }
        const failed = failedTile(event);
        if (failed) failedBeforeSwap.set(`${failed.z}/${failed.x}/${failed.y}`, failed);
        const status = statusOf(event.error);
        if (status === 401 || status === 403) {
            fallBack(`a tile was refused with ${status}`);
            return;
        }
        const now = Date.now();
        failures = failures.filter((at) => now - at < ERROR_WINDOW_MS);
        failures.push(now);
        if (failures.length >= ERROR_THRESHOLD) fallBack(`${failures.length} tiles failed within ${ERROR_WINDOW_MS / 1000}s`);
    };
    swappers.add(swap);
    map.on("error", onError);
    // The page may have fallen back between this map being set up and its style arriving.
    if (pageTemplate === null) swap();
    return () => {
        swappers.delete(swap);
        map.off("error", onError);
    };
}

/**
 * Draws a whole-style MapLibre map's basemap source from the hosted tiles, for as long as they work.
 *
 * For the Leaflet bridge (`map-layers.ts` `baseLayer()`), whose MapLibre map loads the style document itself. The
 * swap is made on `style.load`, which MapLibre fires as the style's sources are added and before any of their
 * tiles are asked for, so no tile is fetched from our own mirror first.
 * @param map - The bridge's MapLibre map, before its style has loaded.
 * @param kind - The base layer key the style draws.
 */
export function useHostedTilesOn(map: MaplibreMap, kind: string): void {
    if (!hostedTilesFor(kind)) return;
    let stop: (() => void) | null = null;
    map.once("style.load", () => {
        const template = hostedTilesFor(kind);
        const own = ownTilesOf(map.getStyle()?.sources?.[BASEMAP_SOURCE_ID]);
        const source = map.getSource(BASEMAP_SOURCE_ID) as { setTiles?: (tiles: string[]) => unknown } | undefined;
        if (!template || !own || typeof source?.setTiles !== "function") return;
        source.setTiles([template]);
        stop = watchHostedTiles(map, BASEMAP_SOURCE_ID, own);
    });
    map.once("remove", () => stop?.());
}

/** Forgets this page's decision and every watcher. Test-only. */
export function resetHostedBasemapForTests(): void {
    pageTemplate = undefined;
    failures = [];
    swappers.clear();
    fallbackListeners.clear();
}
