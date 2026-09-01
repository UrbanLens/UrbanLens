/**
 * Read-only access to the main map's localStorage pin cache
 * (`ul_pins_v5_${profileUuid}`, written by pages/map/index.html's own inline
 * script). Deliberately a small, standalone reader rather than a refactor of
 * that script - the map page's cache read/write/invalidate logic stays
 * exactly as-is, this only parses the same on-disk shape from elsewhere
 * (currently the Tools-page local folder scanner, to skip locations the user
 * already has a pin for).
 *
 * Best-effort only: a missing/stale/disabled cache just means nothing gets
 * filtered here, since the caller always re-checks authoritatively server-side.
 */

// Must match pages/map/index.html's own `_CACHE_KEY`/`v:` literals (that inline
// script is the only writer of this localStorage entry). This constant drifted out
// of sync with that page's cache-version bumps before (last matched v6), which
// silently made every read here return [] since the real payload's `v` never
// matched. Both are exported so pin-cache.contract.test.ts can read the template
// and fail the build when the two sides disagree again, rather than the feature
// just going quiet.
export const PIN_CACHE_VERSION = 9;

/** The localStorage key holding one profile's cached pin store. */
export function pinCacheKey(profileUuid: string): string {
    return `ul_pins_v5_${profileUuid}`;
}

export interface CachedPinLocation {
    latitude: number;
    longitude: number;
}

/** A cached pin's fields relevant to building an instant search suggestion. */
export interface CachedSearchPin {
    uuid: string;
    name: string;
    latitude: number;
    longitude: number;
    icon?: string;
    address?: string;
    tags?: string[];
}

/** Parse the raw per-pin records out of the current profile's cache, or [] if unavailable/invalid. */
function readRawCachedPins(profileUuid: string): Array<Record<string, unknown>> {
    if (!profileUuid) return [];
    try {
        const raw = localStorage.getItem(pinCacheKey(profileUuid));
        if (!raw) return [];
        const cache = JSON.parse(raw);
        if (cache?.v !== PIN_CACHE_VERSION || cache?.profileUuid !== profileUuid) return [];
        const pins = cache.pins;
        if (!pins || typeof pins !== "object") return [];
        return Object.values(pins) as Array<Record<string, unknown>>;
    } catch {
        return [];
    }
}

/** Return the lat/lng of every pin in the current profile's cached pin store, or [] if unavailable. */
export function readCachedPinLocations(profileUuid: string): CachedPinLocation[] {
    const locations: CachedPinLocation[] = [];
    for (const pin of readRawCachedPins(profileUuid)) {
        const lat = Number(pin?.latitude);
        const lng = Number(pin?.longitude);
        if (Number.isFinite(lat) && Number.isFinite(lng)) locations.push({ latitude: lat, longitude: lng });
    }
    return locations;
}

/**
 * Return name/location/tag fields for every cached pin, for building instant
 * (zero-latency) search suggestions while the authoritative server-side
 * autocomplete request is still in flight. Best-effort only - the caller's
 * network request always supersedes this once it resolves.
 */
export function readCachedPinsForSearch(profileUuid: string): CachedSearchPin[] {
    const results: CachedSearchPin[] = [];
    for (const pin of readRawCachedPins(profileUuid)) {
        const lat = Number(pin?.latitude);
        const lng = Number(pin?.longitude);
        const name = typeof pin?.name === "string" ? pin.name : "";
        if (!name || !Number.isFinite(lat) || !Number.isFinite(lng)) continue;
        results.push({
            uuid: typeof pin?.uuid === "string" ? pin.uuid : "",
            name,
            latitude: lat,
            longitude: lng,
            icon: typeof pin?.icon === "string" ? pin.icon : undefined,
            address: typeof pin?.address === "string" ? pin.address : undefined,
            tags: Array.isArray(pin?.tags) ? (pin.tags as unknown[]).filter((t): t is string => typeof t === "string") : undefined,
        });
    }
    return results;
}

/**
 * Every generation of the pin-cache key: `ul_pins_v<N>_<profile id>`.
 *
 * Deliberately not the bare `ul_pins_` prefix - `ul_pins_dirty`, the flag other
 * pages set to force the map to refetch, would match that and get swept away.
 */
const PIN_CACHE_KEY_PATTERN = /^ul_pins_v\d+_/;

/**
 * Delete every pin-cache blob except the one currently in use.
 *
 * The cache is per-profile and per-version, so a browser accumulates blobs that
 * nothing will ever read again: keys from retired versions (v4, and pre-v5 keys
 * built from the profile PK rather than its UUID), and other accounts' blobs
 * from a shared browser. Only the live key is ever read, so the reader's own
 * expiry can never reclaim them - a multi-megabyte orphan just sits in the ~5 MB
 * origin quota until the user manually clears site data, which is why clearing
 * the cache by hand "fixed" a QuotaExceededError that looked like a pin-count
 * limit.
 *
 * Matching on the shared prefix rather than a list of known-dead keys means the
 * next version bump needs no change here.
 *
 * @param currentKey The key to keep - the caller's live cache.
 * @returns How many orphaned entries were removed.
 */
export function purgeForeignPinCaches(currentKey: string): number {
    let removed = 0;
    try {
        const doomed: string[] = [];
        for (let i = 0; i < localStorage.length; i++) {
            const key = localStorage.key(i);
            if (key && key !== currentKey && PIN_CACHE_KEY_PATTERN.test(key)) doomed.push(key);
        }
        for (const key of doomed) {
            try {
                localStorage.removeItem(key);
                removed++;
            } catch {
                // Keep going: one unremovable key must not strand the rest.
            }
        }
    } catch {
        // Storage unavailable (private mode, disabled) - nothing to reclaim.
    }
    return removed;
}

declare global {
    interface Window {
        ulPurgeForeignPinCaches?: typeof purgeForeignPinCaches;
    }
}

/**
 * Expose {@link purgeForeignPinCaches} to the map page's inline cache script,
 * which is not a module and so cannot import it. Installed by core.js, which
 * base.html loads synchronously in <head> - well before that inline script runs.
 */
export function installGlobalPinCachePurge(): void {
    window.ulPurgeForeignPinCaches = purgeForeignPinCaches;
}
