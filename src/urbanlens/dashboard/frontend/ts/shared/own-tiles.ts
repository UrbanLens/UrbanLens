/**
 * Loading tiles this deployment serves itself, shared by every path that draws one.
 *
 * Three do: Leaflet (`map-layers.ts`), MapLibre (`maplibre-raster-style.ts` registers a protocol
 * handler) and the PNG exporter (`map-export.ts`). They agree on one thing that matters - the tile
 * proxy in `controllers/basemap_tiles.py` can only fetch `basemap_tile_upstream_concurrency` tiles
 * per web process at once and answers 503 immediately over that, so the constraint is a property of
 * the deployment rather than of any one renderer. Keeping the pacing and the retry policy here is
 * what stops a fourth consumer from rediscovering it the hard way.
 *
 * Pacing matters more than retrying. A cold viewport is ~30 tiles, the browser asks for all of them
 * at once, and the site's whole budget is 6 upstream fetches - so unpaced, 24 of them are refused,
 * and the burst has to be rebuilt from retries that mostly collide with each other again. Queued to
 * the same width as the budget, the same 30 tiles are 30 requests that all succeed, finishing in
 * about the time the upstream needs anyway. Retries stay as the safety net for the slots this page
 * does not control: other tabs, other users, a restarting worker.
 */

/**
 * Requests for this deployment's own tiles in flight at once, across every map on the page.
 *
 * Matched to the site's upstream budget - `basemap_tile_upstream_concurrency` (2) x
 * `WEB_CONCURRENCY` (3). Going wider only buys refusals; going narrower leaves the upstream idle.
 * One page is not entitled to the whole budget, but it is the only number here worth spending, and
 * a page that asks for less than it can use is slower for no one else's benefit.
 */
const OWN_TILE_CONCURRENCY = 6;

/**
 * Backoff before each retry, in milliseconds - jittered, so a viewport's worth of tiles refused in
 * the same instant does not rebuild that instant a second later.
 *
 * Nothing here is shorter than the `Retry-After: 1` the proxy sends: an upstream fetch takes ~1.5s
 * (`P131`), so a retry inside that window is refused for certain and costs one of only four
 * attempts. Queueing means these are rarely reached at all.
 */
const OWN_TILE_RETRY_DELAYS_MS = [1000, 2500, 5000, 9000];

/** Statuses worth asking again about. Everything else - 404 for a real gap, 403, 401 - is an answer. */
const RETRYABLE_STATUSES = new Set([408, 429, 502, 503, 504]);

/**
 * How long a slot may be held before it is handed back regardless.
 *
 * A slot is released by whoever took it, in a `finally` - but Leaflet can drop a tile element
 * without its `load` or `error` ever firing, and a leaked slot permanently narrows the queue for
 * every map on the page. Over-subscribing briefly is recoverable; a queue that shrinks to nothing
 * is not.
 */
const SLOT_WATCHDOG_MS = 30_000;

let inFlight = 0;
const waiting: Array<() => void> = [];

/** Bumped by the test-only reset, so a slot taken before it cannot hand itself back afterwards. */
let epoch = 0;

/**
 * Whether this URL is served by this deployment rather than by a tile vendor.
 *
 * Own tiles are paced and retried; a vendor's are neither, because a CDN's failure is usually its
 * rate limiter and asking again earns a longer block. The catalogue always builds a path-relative
 * template (`services/map/basemap_catalogue.py`'s `tile_url_template`), and every built-in vendor
 * entry is an absolute `https://` URL, so the distinction is exactly "is this a path".
 * @param url - A tile URL or template.
 */
export function isOwnTileUrl(url: string): boolean {
    return url.startsWith("/") && !url.startsWith("//");
}

/**
 * Takes one of the page's slots for requesting an own tile, waiting for one if all are busy.
 * @returns A release function - idempotent, and safe to call from a `finally`.
 */
export function acquireOwnTileSlot(): Promise<() => void> {
    let released = false;
    let watchdog: ReturnType<typeof setTimeout> | undefined;
    const takenAt = epoch;
    const release = (): void => {
        if (released || takenAt !== epoch) return;
        released = true;
        if (watchdog !== undefined) clearTimeout(watchdog);
        const next = waiting.shift();
        // Handed straight to the next waiter rather than counted down and back up, so a queued
        // request cannot lose its place to one that arrives in between.
        if (next) next();
        else inFlight--;
    };
    const armed = (): (() => void) => {
        watchdog = setTimeout(release, SLOT_WATCHDOG_MS);
        return release;
    };
    if (inFlight < OWN_TILE_CONCURRENCY) {
        inFlight++;
        return Promise.resolve(armed());
    }
    return new Promise((resolve) => waiting.push(() => resolve(armed())));
}

/**
 * The jittered delay before attempt number `attempt`, or `null` once the attempts are spent.
 * @param attempt - How many attempts have already failed.
 */
export function ownTileRetryDelayMs(attempt: number): number | null {
    const delay = OWN_TILE_RETRY_DELAYS_MS[attempt];
    return delay === undefined ? null : delay * (0.5 + Math.random());
}

/** Raised for an answer worth believing - a 404 is a real gap in the layer, not a busy upstream. */
class TerminalTileError extends Error {}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
    // Checked before listening: "abort" does not fire again for a signal already aborted, so a tile
    // cancelled during the previous attempt would otherwise sit out the whole backoff first.
    if (signal?.aborted) return Promise.reject(signal.reason instanceof Error ? signal.reason : new Error("aborted"));
    return new Promise((resolve, reject) => {
        const timer = setTimeout(resolve, ms);
        signal?.addEventListener(
            "abort",
            () => {
                clearTimeout(timer);
                reject(signal.reason instanceof Error ? signal.reason : new Error("aborted"));
            },
            { once: true },
        );
    });
}

/**
 * Fetches one own tile's bytes, queued behind the page's other tiles and retried if the proxy is
 * busy rather than unable.
 * @param url - A path-relative tile URL, already templated.
 * @param signal - Aborts the wait as well as the request, so a tile the map no longer wants stops
 * occupying a slot the tiles it does want are queued for.
 * @throws If every attempt failed, or the proxy gave an answer worth believing.
 */
export async function fetchOwnTile(url: string, signal?: AbortSignal): Promise<ArrayBuffer> {
    let lastError: Error = new Error(`No tile at ${url}`);
    for (let attempt = 0; ; attempt++) {
        if (attempt > 0) {
            const delay = ownTileRetryDelayMs(attempt - 1);
            if (delay === null) throw lastError;
            await sleep(delay, signal);
        }
        const release = await acquireOwnTileSlot();
        try {
            const response = await fetch(url, { signal, headers: { Accept: "image/*" } });
            if (response.ok) return await response.arrayBuffer();
            const error = new Error(`Tile request for ${url} answered ${response.status}`);
            if (!RETRYABLE_STATUSES.has(response.status)) throw new TerminalTileError(error.message);
            lastError = error;
        } catch (error) {
            if (error instanceof TerminalTileError || signal?.aborted) throw error;
            lastError = error instanceof Error ? error : new Error(String(error));
        } finally {
            release();
        }
    }
}

/**
 * Fetches one own tile as something a canvas can draw.
 * @param url - A path-relative tile URL, already templated.
 * @param signal - Passed through to {@link fetchOwnTile}.
 * @returns The decoded tile, or `null` if it could not be had - the exporter draws what it got and
 * a thrown error would cost the whole image rather than one square of it.
 */
export async function loadOwnTileImage(url: string, signal?: AbortSignal): Promise<ImageBitmap | null> {
    try {
        // Same-origin bytes decoded here rather than assigned to an <img src>, so the canvas the
        // exporter draws into is never tainted and stays readable by toDataURL().
        return await createImageBitmap(new Blob([await fetchOwnTile(url, signal)]));
    } catch {
        return null;
    }
}

/** Empties the queue and the in-flight count so one test's pacing cannot decide another's. Test-only. */
export function resetOwnTileGateForTests(): void {
    epoch++;
    inFlight = 0;
    waiting.length = 0;
}
