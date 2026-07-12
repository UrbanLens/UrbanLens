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

const CACHE_VERSION = 6;

export interface CachedPinLocation {
    latitude: number;
    longitude: number;
}

/** Return the lat/lng of every pin in the current profile's cached pin store, or [] if unavailable. */
export function readCachedPinLocations(profileUuid: string): CachedPinLocation[] {
    if (!profileUuid) return [];
    try {
        const raw = localStorage.getItem(`ul_pins_v5_${profileUuid}`);
        if (!raw) return [];
        const cache = JSON.parse(raw);
        if (cache?.v !== CACHE_VERSION || cache?.profileUuid !== profileUuid) return [];
        const pins = cache.pins;
        if (!pins || typeof pins !== "object") return [];
        const locations: CachedPinLocation[] = [];
        for (const pin of Object.values(pins) as Array<Record<string, unknown>>) {
            const lat = Number(pin?.latitude);
            const lng = Number(pin?.longitude);
            if (Number.isFinite(lat) && Number.isFinite(lng)) locations.push({ latitude: lat, longitude: lng });
        }
        return locations;
    } catch {
        return [];
    }
}
