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

// Must match pages/map/index.html's own `_CACHE_KEY`/`v: 8` (the only writer
// of this localStorage entry) - this constant drifted out of sync with that
// page's cache-version bumps before (last matched v6), which silently made
// every read here return [] since the real payload's `v` never matched.
const CACHE_VERSION = 8;

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
        const raw = localStorage.getItem(`ul_pins_v5_${profileUuid}`);
        if (!raw) return [];
        const cache = JSON.parse(raw);
        if (cache?.v !== CACHE_VERSION || cache?.profileUuid !== profileUuid) return [];
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
