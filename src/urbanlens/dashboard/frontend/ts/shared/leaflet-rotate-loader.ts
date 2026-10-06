/**
 * Loads leaflet-rotate on demand, for the maps that turn: the composer, and a saved map drawn turned.
 *
 * Not loaded with Leaflet on every page, because it patches Leaflet itself: every map built after it
 * runs gets its rotate control, its shift-and-scroll turning and its touch handler unless told
 * otherwise. Loading it only when a turning map is wanted keeps every other page as it was, and once
 * it has run, those defaults are put back so only a map that asks for rotation gets any of it.
 */

// `L` is a page global from a CDN <script>; see markup-engine.ts.
declare const L: typeof import("leaflet");

/** Where the script comes from - `vendor_asset_source("leaflet_rotate_js")`, via the page's config. */
export interface ScriptSource {
    src: string;
    integrity?: string;
}

/** What leaflet-rotate adds to a map, which @types/leaflet does not know about. */
export interface RotatableMap {
    setBearing: (degrees: number) => void;
    getBearing: () => number;
    options: { rotate?: boolean };
}

/** Options a map that turns is built with. */
export interface RotateMapOptions {
    rotate: true;
    bearing: number;
    touchRotate: boolean;
    shiftKeyRotate: boolean;
    rotateControl: false;
}

/** Long enough for a cold CDN; past it the map opens without rotation rather than not at all. */
const LOAD_TIMEOUT_MS = 6000;

let loading: Promise<boolean> | null = null;

/** Whether leaflet-rotate has patched this page's Leaflet. */
export function rotationAvailable(): boolean {
    return typeof L !== "undefined" && typeof (L.Map?.prototype as Partial<RotatableMap> | undefined)?.setBearing === "function";
}

/** Whether `map` was built to turn - leaflet-rotate being loaded is not enough on its own. */
export function canTurn(map: L.Map): map is L.Map & RotatableMap {
    const turnable = map as L.Map & Partial<RotatableMap>;
    return turnable.options?.rotate === true && typeof turnable.setBearing === "function" && typeof turnable.getBearing === "function";
}

/** Puts back Leaflet's own defaults for the options leaflet-rotate turns on for every map. */
function restoreDefaults(): void {
    // Only a map built with `rotate: true` should turn. Left alone, every later map on the page
    // would grow a rotate control and take shift-and-scroll as a turn - a thumbnail included.
    L.Map.mergeOptions({ rotate: false, bearing: 0, rotateControl: false, shiftKeyRotate: false, touchRotate: false, compassBearing: false, bounceAtZoomLimits: true });
}

/**
 * Loads leaflet-rotate once, however many maps ask.
 * @param source - The script's address and its integrity hash.
 * @returns Whether rotation is available - false when Leaflet is missing, the script could not be
 *   fetched, or it did not arrive in time. A caller builds its map either way.
 */
export function ensureLeafletRotate(source: ScriptSource | null | undefined): Promise<boolean> {
    if (typeof L === "undefined" || typeof document === "undefined") return Promise.resolve(false);
    if (rotationAvailable()) return Promise.resolve(true);
    if (!source?.src) return Promise.resolve(false);
    loading ??= new Promise<boolean>((resolve) => {
        const script = document.createElement("script");
        script.src = source.src;
        if (source.integrity) {
            script.integrity = source.integrity;
            script.crossOrigin = "anonymous";
        }
        const timer = window.setTimeout(() => finish(false), LOAD_TIMEOUT_MS);
        function finish(ok: boolean): void {
            window.clearTimeout(timer);
            if (ok && rotationAvailable()) {
                restoreDefaults();
                resolve(true);
            } else {
                // A failed load is not kept: the next map to ask tries again.
                loading = null;
                resolve(false);
            }
        }
        script.addEventListener("load", () => finish(true), { once: true });
        script.addEventListener("error", () => finish(false), { once: true });
        document.head.appendChild(script);
    });
    return loading;
}

/** Map options for a map that turns, at `compassBearing` - or none, where rotation is not available. */
export function rotateOptions(compassBearing: number): RotateMapOptions | Record<string, never> {
    if (!rotationAvailable()) return {};
    return { rotate: true, bearing: toContentTurn(compassBearing), touchRotate: true, shiftKeyRotate: true, rotateControl: false };
}

/**
 * leaflet-rotate's bearing is how far the map's content is turned clockwise; a compass bearing (what
 * is saved, and what MapLibre means) is which direction is up. They are the same turn in opposite
 * senses.
 */
export function toContentTurn(compassBearing: number): number {
    const value = Number.isFinite(compassBearing) ? compassBearing : 0;
    return (((360 - value) % 360) + 360) % 360;
}

/** The compass bearing - which direction is up - of a map whose content is turned `contentTurn` clockwise. */
export function toCompassBearing(contentTurn: number): number {
    return toContentTurn(contentTurn);
}

/** Forgets a load in progress. Test-only. */
export function resetLeafletRotateLoaderForTests(): void {
    loading = null;
}
