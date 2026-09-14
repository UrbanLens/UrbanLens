/**
 * Read-only access to the main map's localStorage pin cache.
 */

// Must match pages/map/index.html's own `_CACHE_KEY`/`v:` literals (that inline script is the only writer of this localStorage entry).
export const PIN_CACHE_VERSION = 11;

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

/** A cached pin's label ids resolved against the blob's own label dictionary. */
interface CachedStore {
    pins: Array<Record<string, unknown>>;
    labels: Record<string, { name?: unknown }>;
}

/** Parse the current profile's cache, or an empty store if unavailable/invalid. */
function readCachedStore(profileUuid: string): CachedStore {
    const empty: CachedStore = { pins: [], labels: {} };
    if (!profileUuid) return empty;
    try {
        const raw = localStorage.getItem(pinCacheKey(profileUuid));
        if (!raw) return empty;
        const cache = JSON.parse(raw);
        if (cache?.v !== PIN_CACHE_VERSION || cache?.profileUuid !== profileUuid) return empty;
        const pins = cache.pins;
        if (!pins || typeof pins !== "object") return empty;
        const labels = cache.labels && typeof cache.labels === "object" ? cache.labels : {};
        return { pins: Object.values(pins) as Array<Record<string, unknown>>, labels };
    } catch {
        return empty;
    }
}

/** Parse the raw per-pin records out of the current profile's cache, or [] if unavailable/invalid. */
function readRawCachedPins(profileUuid: string): Array<Record<string, unknown>> {
    return readCachedStore(profileUuid).pins;
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
 * Return name/location/tag fields for every cached pin, for building instant (zero-latency) search suggestions while the authoritative.
 */
export function readCachedPinsForSearch(profileUuid: string): CachedSearchPin[] {
    const results: CachedSearchPin[] = [];
    const { pins, labels } = readCachedStore(profileUuid);
    for (const pin of pins) {
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
            tags: Array.isArray(pin?.label_ids)
                ? (pin.label_ids as unknown[])
                      .map((id) => labels[String(id)]?.name)
                      .filter((name): name is string => typeof name === "string" && name.length > 0)
                : undefined,
        });
    }
    return results;
}

/**
 * Every generation of the pin-cache key: `ul_pins_v<N>_<profile id>`.
 */
const PIN_CACHE_KEY_PATTERN = /^ul_pins_v\d+_/;

/**
 * Delete every pin-cache blob except the one currently in use.
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
