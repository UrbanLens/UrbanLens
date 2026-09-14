/**
 * Beta "time slider" overlay: lets a user scrub a pin's/wiki's map through years and see OpenHistoricalMap (OHM) vector features.
 */

// Leaflet is loaded via a CDN <script> tag on map pages (see map-layers.ts for
// why this is an ambient global rather than an import).
declare const L: typeof import("leaflet");

export interface TemporalImagerySliderOptions {
    /** The #temporal-imagery-slider root element (icon/input/label live inside it). */
    container: HTMLElement;
    /** Years OHM has dated coverage for near this location, per temporal_slider_years(). */
    years: number[];
    /** Server-built URL containing the literal placeholder "9999" for the year. */
    urlTemplate: string;
    /** Reports a fetch failure so the page can toast it - this module is toast-library-agnostic. */
    onError?: (message: string) => void;
}

export interface TemporalImagerySliderInstance {
    /** Scrubs to a year programmatically, as if the user had released the slider there. */
    setYear: (year: number) => void;
    /** The currently selected year (the slider's live position). */
    getYear: () => number;
}

/** Milliseconds to wait after the slider settles before fetching that year's features. */
const FETCH_DEBOUNCE_MS = 350;

/**
 * The slider label's only two states: the plain year, or "Today" at the
 * live-map position (max), which carries no historical overlay.
 */
export function formatYearLabel(year: number, maxYear: number): string {
    return year >= maxYear ? "Today" : String(year);
}

/**
 * Substitutes the TEMPORAL_YEAR_PLACEHOLDER (9999) in a server-built URL template with the chosen year.
 */
export function temporalFeaturesUrl(urlTemplate: string, year: number): string {
    return urlTemplate.replace(/9999\/$/, `${year}/`);
}

/**
 * The slider's min/max bounds: the earliest year OHM has coverage for, through
 * the current calendar year - "today" (no overlay, the live map) is always the
 * top of the range even on the rare chance OHM's own coverage lags behind it.
 */
export function sliderRange(years: number[], currentYear: number): { min: number; max: number } {
    const earliest = Math.min(...years);
    return { min: Math.min(earliest, currentYear), max: currentYear };
}

interface TemporalFeaturesResponse {
    year: number;
    geojson: GeoJSON.FeatureCollection;
}

// Dashed amber/orange stroke - visually distinct from the live basemap and from this file's sibling modules' own accent colors.
const OHM_OVERLAY_STYLE: L.PathOptions = {
    color: "#f97316",
    weight: 2.5,
    dashArray: "6 4",
    fillColor: "#f97316",
    fillOpacity: 0.12,
};

/**
 * Wires the compact below-the-map time slider to a Leaflet map.
 * @param map - The Leaflet map to overlay OHM features on.
 * @param options - See {@link TemporalImagerySliderOptions}.
 * @returns The slider's control instance, or null when there are no years to
 */
export function createTemporalImagerySlider(map: L.Map, options: TemporalImagerySliderOptions): TemporalImagerySliderInstance | null {
    const { container, years, urlTemplate, onError } = options;
    if (years.length === 0) return null;

    const inputEl = container.querySelector<HTMLInputElement>("#temporal-imagery-slider-input");
    const labelEl = container.querySelector<HTMLElement>("#temporal-imagery-slider-label");
    if (!inputEl || !labelEl) return null;
    // Rebound to plain, definitely-non-null consts.
    const input = inputEl;
    const label = labelEl;

    const currentYear = new Date().getFullYear();
    const { min, max } = sliderRange(years, currentYear);
    input.min = String(min);
    input.max = String(max);
    input.value = String(max);
    label.textContent = formatYearLabel(max, max);

    const cache = new Map<number, GeoJSON.FeatureCollection>();
    let activeLayer: L.GeoJSON | null = null;
    let debounceTimer: ReturnType<typeof setTimeout> | null = null;
    // Reference-counted rather than a plain boolean.
    let pendingFetches = 0;

    function setLoading(loading: boolean): void {
        container.classList.toggle("temporal-imagery-slider--loading", loading);
    }

    function clearOverlay(): void {
        if (!activeLayer) return;
        map.removeLayer(activeLayer);
        activeLayer = null;
    }

    function showOverlay(geojson: GeoJSON.FeatureCollection): void {
        clearOverlay();
        activeLayer = L.geoJSON(geojson, { style: OHM_OVERLAY_STYLE }).addTo(map);
    }

    async function fetchYear(year: number): Promise<void> {
        const cached = cache.get(year);
        if (cached) {
            showOverlay(cached);
            return;
        }
        pendingFetches += 1;
        setLoading(true);
        try {
            const response = await fetch(temporalFeaturesUrl(urlTemplate, year));
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = (await response.json()) as TemporalFeaturesResponse;
            cache.set(year, data.geojson);
            // The user may have already scrubbed elsewhere while this was in flight.
            if (Number(input.value) === year) showOverlay(data.geojson);
        } catch {
            onError?.("Couldn't load historical map data for that year.");
        } finally {
            pendingFetches -= 1;
            setLoading(pendingFetches > 0);
        }
    }

    // Cheap, no fetch: live-update the label on every tick.
    input.addEventListener("input", () => {
        label.textContent = formatYearLabel(Number(input.value), max);
    });

    // Fires on release/keyup-commit, not every tick.
    input.addEventListener("change", () => {
        const year = Number(input.value);
        if (debounceTimer) {
            clearTimeout(debounceTimer);
            debounceTimer = null;
        }
        if (year === max) {
            // Back to "Today" - revert to the live basemap, no fetch needed.
            clearOverlay();
            return;
        }
        debounceTimer = setTimeout(() => {
            debounceTimer = null;
            void fetchYear(year);
        }, FETCH_DEBOUNCE_MS);
    });

    return {
        setYear: (year: number) => {
            const clamped = Math.min(Math.max(year, min), max);
            input.value = String(clamped);
            label.textContent = formatYearLabel(clamped, max);
            input.dispatchEvent(new Event("change"));
        },
        getYear: () => Number(input.value),
    };
}
