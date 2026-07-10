/**
 * Shared map layers component - the single source of truth for every Leaflet
 * map on the site: tile sources (street / dark / topographic / satellite),
 * overlays (weather, geopolitical borders), the Google-Maps-style layers
 * flyout panel, dark map mode, layer persistence, and footer attribution.
 *
 * Extracted verbatim from the main map (pages/map/index.html), which defines
 * the canonical UI and behavior. Every other map (pin detail, Location wiki,
 * safety check-in, trips, the comment map composer/viewer) drives the same
 * engine so layer behavior is guaranteed identical site-wide.
 *
 * Server-side counterpart: dashboard/templatetags/map_components.py renders
 * the panel markup ({% map_layers_panel %}) that `create()` binds to via
 * data attributes:
 *   [data-map-layers-panel]  panel/strip root
 *   [data-layers-toggle]     flyout open/close button (full panel variant)
 *   [data-layers-menu]       flyout menu (full panel variant)
 *   [data-map-layer="key"]   individual layer button
 *   [data-layer-kind]        "base" | "overlay" | "action" | "custom"
 *
 * Exposed globally as `window.MapLayers` (see entries-classic/core.ts) so the
 * classic inline scripts in templates can use it without a module import.
 */

// Leaflet is loaded via a CDN <script> tag on map pages, so it must be typed
// as an ambient global rather than imported (importing would bundle a second
// copy and clobber CDN plugins hung off window.L).
declare const L: typeof import("leaflet");

export type BaseLayerKey = "street" | "topographic" | "satellite";
export type MapDarkMode = "light" | "dark" | "system";

interface TileDef {
    url: string;
    options: L.TileLayerOptions;
}

/**
 * Canonical tile sources. maxNativeZoom caps tile requests at each provider's
 * real depth while maxZoom lets Leaflet upscale beyond it (Google-like) so a
 * layer never drops out when the user zooms past the native depth.
 */
const TILE_DEFS: Record<string, TileDef> = {
    street: {
        url: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        options: {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
            maxNativeZoom: 19,
            maxZoom: 21,
        },
    },
    dark: {
        url: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        options: {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
            maxNativeZoom: 20,
            maxZoom: 21,
        },
    },
    topographic: {
        url: "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        options: {
            attribution: "&copy; OpenTopoMap contributors",
            // OpenTopoMap only renders tiles up to zoom 17; upscale beyond that.
            maxNativeZoom: 17,
            maxZoom: 21,
        },
    },
    satellite: {
        url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        options: {
            attribution: "Tiles &copy; Esri &mdash; Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, Getmapping, Aerogrid, IGN, IGP, UPR-EGP, and the GIS User Community",
            maxNativeZoom: 19,
            maxZoom: 21,
        },
    },
    borders: {
        url: "https://services.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
        options: {
            attribution: "Boundaries &copy; Esri",
            maxNativeZoom: 19,
            maxZoom: 21,
            opacity: 0.6,
            pane: "overlayPane",
        },
    },
};

/**
 * Legacy layer-mode aliases accepted defensively (pre-canonical MarkupMap
 * values and old cached snapshots). Mirrors LEGACY_LAYER_MODE_ALIASES in
 * dashboard/models/markup/meta.py.
 */
const BASE_ALIASES: Record<string, BaseLayerKey> = {
    street: "street",
    standard: "street",
    osm: "street",
    topographic: "topographic",
    topo: "topographic",
    terrain: "topographic",
    satellite: "satellite",
};

/**
 * Normalizes any historical base-layer identifier ("standard", "topo", ...)
 * to the canonical key used by this module.
 */
export function normalizeBase(key: string | null | undefined): BaseLayerKey {
    return BASE_ALIASES[(key || "").toLowerCase()] || "street";
}


/**
 * Creates a tile layer for one of the canonical sources.
 *
 * @param kind - Canonical or legacy source key ("street", "standard", "satellite", "topo", "dark", ...).
 * @param extraOptions - Leaflet options merged over the canonical defaults (e.g. pane).
 */
export function tileLayer(kind: string, extraOptions?: L.TileLayerOptions): L.TileLayer {
    const def = TILE_DEFS[kind] || TILE_DEFS[normalizeBase(kind)] || TILE_DEFS.street!;
    return L.tileLayer(def.url, { ...def.options, ...extraOptions });
}

/** Creates the geopolitical borders overlay (same tiles on every map). */
export function bordersOverlay(): L.TileLayer {
    return tileLayer("borders");
}

/** Creates the OpenWeatherMap rain + clouds overlay pair. */
export function weatherLayers(apiKey: string): { rain: L.TileLayer; clouds: L.TileLayer } {
    const attribution = 'Map data &copy; <a href="https://openweathermap.org">OpenWeatherMap</a>';
    const make = (layer: string, opacity: number) =>
        L.tileLayer(`https://tile.openweathermap.org/map/${layer}/{z}/{x}/{y}.png?appid=${apiKey}`, { attribution, opacity, maxZoom: 21 });
    return { rain: make("precipitation_new", 0.7), clouds: make("clouds_new", 0.5) };
}

export interface MapLayersState {
    base: BaseLayerKey;
    weather: boolean;
    borders: boolean;
    darkMode: MapDarkMode;
}

export interface CustomLayerToggle {
    /** Whether the layer/feature is currently on (drives the button's active state). */
    isActive: () => boolean;
    /** Toggles the layer/feature; button state re-syncs right after. */
    toggle: () => void;
    /**
     * When true the button's .active class means "feature OFF" (the main
     * map's pins button highlights when pins are hidden).
     */
    activeWhenOff?: boolean;
}

export interface MapLayersOptions {
    /**
     * Panel/strip root element or selector for this map's layer buttons.
     * Always pass it explicitly when the page has a panel - multiple maps
     * (and the comment-map composer in base.html) can coexist on one page,
     * so a document-wide default would be ambiguous. Omit for headless use
     * (no buttons; layers driven via the returned instance methods).
     */
    root?: HTMLElement | string | null;
    /** OpenWeatherMap API key; the weather button is hidden when absent. */
    apiKey?: string | null;
    /** Map dark mode preference. Default "light". */
    darkMode?: MapDarkMode;
    /**
     * Initial base layer: "street" | "topographic" | "satellite" (legacy
     * aliases accepted) or "remember" to restore the last state persisted
     * under `storageKey`.
     */
    defaultBase?: string | null;
    /** Overlays active at startup, e.g. ["borders"] or ["weather"]. */
    initialOverlays?: string[];
    /** localStorage key used when defaultBase === "remember". */
    storageKey?: string | null;
    /**
     * Pane name for topographic tiles so dark mode can invert them without
     * touching other layers. The pane is created (zIndex 401) if missing.
     */
    topoPane?: string | null;
    /** Element that gets a .tiles-loading class while base tiles download. */
    loadingTarget?: HTMLElement | null;
    /** Element that gets data-map-style="dark|light" (default: the map container). */
    styleTarget?: HTMLElement | null;
    /** Receives the combined attribution line whenever active layers change. */
    onAttribution?: ((text: string) => void) | null;
    /** Fired after any base/overlay/dark change with the full current state. */
    onStateChange?: ((state: MapLayersState) => void) | null;
    /** Fired when the user toggles dark mode (persist server-side here). */
    onDarkModeChange?: ((mode: MapDarkMode) => void) | null;
    /** Page-specific toggles keyed by their button's data-map-layer value (pins, places, details, photos, ...). */
    custom?: Record<string, CustomLayerToggle>;
}

export interface MapLayersInstance {
    /** Switches to the given base layer (street removes topo/satellite). */
    setBase: (key: string) => void;
    /** Button semantics: street selects street; topo/satellite toggle themselves off back to street. */
    toggleBase: (key: string) => void;
    toggleWeather: () => void;
    toggleBorders: () => void;
    /** Sets an overlay ("weather" | "borders") to an explicit on/off state. */
    setOverlay: (key: string, on: boolean) => void;
    /** Toggles a page-specific layer registered via options.custom. */
    toggleCustom: (key: string) => void;
    /** Registers (or replaces) a custom toggle after creation. */
    registerToggle: (key: string, toggle: CustomLayerToggle) => void;
    toggleDark: () => void;
    /** Applies a dark mode without persisting (dev toolbar hook). */
    setDarkMode: (mode: MapDarkMode) => void;
    isDarkActive: () => boolean;
    openPanel: () => void;
    closePanel: () => void;
    togglePanel: () => void;
    isPanelOpen: () => boolean;
    /** Re-syncs every button's active state from the actual layer state. */
    syncButtons: () => void;
    getState: () => MapLayersState;
    /** Currently selected base layer key. */
    baseKey: () => BaseLayerKey;
}

const PANEL_TRANSITION_MS = 220;

/**
 * Creates the layers engine for a map and binds it to the rendered panel.
 *
 * @param map - The Leaflet map instance.
 * @param options - Behavior configuration; see MapLayersOptions.
 * @returns The engine instance driving both the layers and the panel buttons.
 */
export function createMapLayers(map: L.Map, options: MapLayersOptions = {}): MapLayersInstance {
    const opts = options;
    const root: HTMLElement | null =
        typeof opts.root === "string" ? document.querySelector<HTMLElement>(opts.root) : (opts.root ?? null);

    let darkMode: MapDarkMode = opts.darkMode || "light";
    const custom: Record<string, CustomLayerToggle> = { ...(opts.custom || {}) };

    // -- Panes -----------------------------------------------------------------
    // Dedicated pane for topo tiles so the dark-mode invert filter never
    // touches satellite or street.
    const topoPaneName = opts.topoPane === undefined ? "topoPane" : opts.topoPane;
    if (topoPaneName && !map.getPane(topoPaneName)) {
        map.createPane(topoPaneName).style.zIndex = "401";
    }

    // -- Layers ------------------------------------------------------------------
    const streetLayer = tileLayer("street");
    const darkLayer = tileLayer("dark");
    const topographicLayer = tileLayer("topographic", topoPaneName ? { pane: topoPaneName } : undefined);
    const satelliteLayer = tileLayer("satellite");
    const bordersLayer = bordersOverlay();
    const weather = opts.apiKey ? weatherLayers(opts.apiKey) : null;

    // -- Dark map mode -----------------------------------------------------------
    function isDarkActive(): boolean {
        if (darkMode === "dark") return true;
        if (darkMode === "light") return false;
        return window.matchMedia("(prefers-color-scheme: dark)").matches;
    }

    // Apply the invert filter to the topo pane when dark map mode is active.
    function applyTopoFilter(): void {
        if (!topoPaneName) return;
        const pane = map.getPane(topoPaneName);
        if (!pane) return;
        pane.style.filter = isDarkActive() && map.hasLayer(topographicLayer)
            ? "invert(100%) hue-rotate(180deg) brightness(90%)"
            : "";
    }

    // Expose the effective map style for SCSS (e.g. #map[data-map-style="dark"]).
    function syncStyleAttribute(): void {
        const target = opts.styleTarget ?? map.getContainer();
        target.dataset.mapStyle = isDarkActive() ? "dark" : "light";
    }

    // Swap between streetLayer and darkLayer without touching satellite/topo.
    // street-or-dark is the always-present bottom base; topo/satellite sit on top.
    function syncBaseLayer(): void {
        if (isDarkActive()) {
            if (map.hasLayer(streetLayer)) map.removeLayer(streetLayer);
            if (!map.hasLayer(darkLayer)) darkLayer.addTo(map);
        } else {
            if (map.hasLayer(darkLayer)) map.removeLayer(darkLayer);
            if (!map.hasLayer(streetLayer)) streetLayer.addTo(map);
        }
        applyTopoFilter();
        syncStyleAttribute();
    }

    // Re-apply the topo filter when the topo layer itself is toggled.
    map.on("layeradd layerremove", (e: L.LayerEvent) => {
        if (e.layer === topographicLayer) applyTopoFilter();
    });

    // In system mode, re-sync whenever the OS preference changes.
    if (darkMode === "system") {
        window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
            syncBaseLayer();
            syncButtons();
        });
    }

    // -- State / persistence -------------------------------------------------------
    function baseKey(): BaseLayerKey {
        if (map.hasLayer(satelliteLayer)) return "satellite";
        if (map.hasLayer(topographicLayer)) return "topographic";
        return "street";
    }

    function getState(): MapLayersState {
        return {
            base: baseKey(),
            weather: !!weather && (map.hasLayer(weather.rain) || map.hasLayer(weather.clouds)),
            borders: map.hasLayer(bordersLayer),
            darkMode,
        };
    }

    const remember = opts.defaultBase === "remember" && !!opts.storageKey;

    function persistState(): void {
        if (remember) {
            try {
                const state = getState();
                localStorage.setItem(opts.storageKey!, JSON.stringify({ base: state.base, weather: state.weather }));
            } catch {
                /* storage unavailable - ignore */
            }
        }
        opts.onStateChange?.(getState());
    }

    // -- Attribution ---------------------------------------------------------------
    // Replaces Leaflet's on-map control on pages that render attribution
    // elsewhere (e.g. the main map's footer).
    function attributionText(): string {
        const parts: string[] = [];
        if (map.hasLayer(satelliteLayer)) {
            parts.push("© Esri");
        } else if (map.hasLayer(topographicLayer)) {
            parts.push("© OpenTopoMap");
        } else if (map.hasLayer(darkLayer)) {
            parts.push("© OSM · CARTO");
        } else {
            parts.push("© OpenStreetMap");
        }
        if (weather && (map.hasLayer(weather.rain) || map.hasLayer(weather.clouds))) {
            parts.push("© OpenWeatherMap");
        }
        if (map.hasLayer(bordersLayer) && !map.hasLayer(satelliteLayer)) {
            parts.push("© Esri");
        }
        parts.push("Leaflet");
        return parts.join(" · ");
    }

    if (opts.onAttribution) {
        map.on("layeradd layerremove", () => opts.onAttribution!(attributionText()));
    }

    // -- Tile loading visual feedback -------------------------------------------------
    // Grey-dim the target while base tiles download; restore when done. Each
    // layer tracks loading via a counter so swapping layers never leaves the
    // map permanently dimmed.
    if (opts.loadingTarget) {
        const target = opts.loadingTarget;
        let loadingCount = 0;
        const onLoading = () => {
            loadingCount++;
            target.classList.add("tiles-loading");
        };
        const onLoad = () => {
            loadingCount = Math.max(0, loadingCount - 1);
            if (loadingCount === 0) target.classList.remove("tiles-loading");
        };
        for (const layer of [streetLayer, topographicLayer, satelliteLayer, darkLayer]) {
            layer.on("loading", onLoading);
            layer.on("load", onLoad);
            layer.on("error", onLoad);
        }
    }

    // -- Button syncing ---------------------------------------------------------------
    function layerButton(key: string): HTMLElement | null {
        return root?.querySelector<HTMLElement>(`[data-map-layer="${key}"]`) ?? null;
    }

    function syncButtons(): void {
        if (!root) return;
        const state = getState();
        layerButton("street")?.classList.toggle("active", state.base === "street");
        layerButton("terrain")?.classList.toggle("active", state.base === "topographic");
        layerButton("satellite")?.classList.toggle("active", state.base === "satellite");
        layerButton("weather")?.classList.toggle("active", state.weather);
        layerButton("borders")?.classList.toggle("active", state.borders);
        layerButton("dark")?.classList.toggle("active", isDarkActive());
        for (const [key, toggle] of Object.entries(custom)) {
            const active = toggle.activeWhenOff ? !toggle.isActive() : toggle.isActive();
            layerButton(key)?.classList.toggle("active", active);
        }
    }

    // -- Base / overlay switching --------------------------------------------------------
    function setBase(rawKey: string): void {
        const key = normalizeBase(rawKey);
        if (key !== "satellite" && map.hasLayer(satelliteLayer)) map.removeLayer(satelliteLayer);
        if (key !== "topographic" && map.hasLayer(topographicLayer)) map.removeLayer(topographicLayer);
        if (key === "satellite" && !map.hasLayer(satelliteLayer)) satelliteLayer.addTo(map);
        if (key === "topographic" && !map.hasLayer(topographicLayer)) topographicLayer.addTo(map);
        syncButtons();
        persistState();
    }

    // Button semantics from the main map: street always selects street;
    // topo/satellite toggle themselves (falling back to street) and are
    // mutually exclusive.
    function toggleBase(rawKey: string): void {
        const key = normalizeBase(rawKey);
        if (key !== "street") {
            const layer = key === "satellite" ? satelliteLayer : topographicLayer;
            if (map.hasLayer(layer)) {
                setBase("street");
                return;
            }
        }
        setBase(key);
    }

    function toggleWeather(): void {
        if (!weather) return;
        if (map.hasLayer(weather.rain) || map.hasLayer(weather.clouds)) {
            map.removeLayer(weather.rain);
            map.removeLayer(weather.clouds);
        } else {
            weather.rain.addTo(map);
            weather.clouds.addTo(map);
        }
        syncButtons();
        persistState();
    }

    function toggleBorders(): void {
        if (map.hasLayer(bordersLayer)) map.removeLayer(bordersLayer);
        else bordersLayer.addTo(map);
        syncButtons();
        persistState();
    }

    function setOverlay(key: string, on: boolean): void {
        if (key === "weather") {
            if (!weather) return;
            const active = map.hasLayer(weather.rain) || map.hasLayer(weather.clouds);
            if (active !== on) toggleWeather();
        } else if (key === "borders") {
            if (map.hasLayer(bordersLayer) !== on) toggleBorders();
        }
    }

    function toggleCustom(key: string): void {
        custom[key]?.toggle();
        syncButtons();
    }

    function registerToggle(key: string, toggle: CustomLayerToggle): void {
        custom[key] = toggle;
        syncButtons();
    }

    function setDarkMode(mode: MapDarkMode): void {
        darkMode = mode;
        syncBaseLayer();
        syncButtons();
    }

    function toggleDark(): void {
        const newMode: MapDarkMode = darkMode === "dark" ? "light" : "dark";
        setDarkMode(newMode);
        opts.onDarkModeChange?.(newMode);
        persistState();
    }

    // -- Flyout panel open/close -------------------------------------------------------
    const toggleBtn = root?.querySelector<HTMLElement>("[data-layers-toggle]") ?? null;
    const menu = root?.querySelector<HTMLElement>("[data-layers-menu]") ?? null;
    let panelCloseTimer: ReturnType<typeof setTimeout> | null = null;

    function isPanelOpen(): boolean {
        return root?.classList.contains("is-open") ?? false;
    }

    function closePanel(): void {
        if (!root || !root.classList.contains("is-open")) return;
        root.classList.remove("is-open");
        if (toggleBtn) {
            toggleBtn.classList.remove("active");
            toggleBtn.setAttribute("aria-expanded", "false");
        }
        if (menu) {
            menu.setAttribute("aria-hidden", "true");
            let closed = false;
            const finishClose = (e?: Event) => {
                if (e && e.target !== menu) return;
                if (closed || root.classList.contains("is-open")) return;
                closed = true;
                if (panelCloseTimer) {
                    clearTimeout(panelCloseTimer);
                    panelCloseTimer = null;
                }
                menu.hidden = true;
                menu.removeEventListener("transitionend", finishClose);
            };
            menu.addEventListener("transitionend", finishClose);
            panelCloseTimer = setTimeout(finishClose, PANEL_TRANSITION_MS + 40);
        }
    }

    function openPanel(): void {
        if (!root) return;
        if (panelCloseTimer) {
            clearTimeout(panelCloseTimer);
            panelCloseTimer = null;
        }
        if (menu) {
            menu.hidden = false;
            menu.setAttribute("aria-hidden", "false");
            // Force a synchronous reflow so the opening transition plays from
            // the hidden state instead of snapping.
            void menu.offsetWidth;
        }
        root.classList.add("is-open");
        if (toggleBtn) {
            toggleBtn.classList.add("active");
            toggleBtn.setAttribute("aria-expanded", "true");
        }
    }

    function togglePanel(): void {
        if (isPanelOpen()) closePanel();
        else openPanel();
    }

    if (toggleBtn) {
        toggleBtn.addEventListener("click", togglePanel);
        document.addEventListener("click", (e) => {
            if (root && !root.contains(e.target as Node)) closePanel();
        });
    }

    // -- Button wiring ---------------------------------------------------------------------
    if (root) {
        root.querySelectorAll<HTMLElement>("[data-map-layer]").forEach((btn) => {
            const key = btn.dataset.mapLayer!;
            const kind = btn.dataset.layerKind || "custom";
            if (key === "weather" && !weather) {
                // No API key configured - the feature can't work, so don't offer it.
                btn.hidden = true;
                return;
            }
            btn.addEventListener("click", () => {
                if (kind === "base") toggleBase(key === "terrain" ? "topographic" : key);
                else if (key === "weather") toggleWeather();
                else if (key === "borders") toggleBorders();
                else if (key === "dark") toggleDark();
                else toggleCustom(key);
            });
        });
    }

    // -- Initial state -------------------------------------------------------------------------
    syncBaseLayer();
    (function applyInitialLayers() {
        let base = opts.defaultBase || "street";
        let weatherOn = (opts.initialOverlays || []).includes("weather");
        const bordersOn = (opts.initialOverlays || []).includes("borders");

        if (base === "remember") {
            base = "street";
            try {
                const saved = JSON.parse(localStorage.getItem(opts.storageKey || "") || "null");
                if (saved) {
                    base = saved.base || "street";
                    weatherOn = !!saved.weather;
                }
            } catch {
                /* corrupt storage - fall back to street */
            }
        }

        const key = normalizeBase(base);
        if (key === "satellite") satelliteLayer.addTo(map);
        else if (key === "topographic") topographicLayer.addTo(map);

        if (weatherOn && weather) {
            weather.rain.addTo(map);
            weather.clouds.addTo(map);
        }
        if (bordersOn) bordersLayer.addTo(map);
        syncButtons();
    })();

    return {
        setBase,
        toggleBase,
        toggleWeather,
        toggleBorders,
        setOverlay,
        toggleCustom,
        registerToggle,
        toggleDark,
        setDarkMode,
        isDarkActive,
        openPanel,
        closePanel,
        togglePanel,
        isPanelOpen,
        syncButtons,
        getState,
        baseKey,
    };
}

export const MapLayers = {
    create: createMapLayers,
    tileLayer,
    bordersOverlay,
    weatherLayers,
    normalizeBase,
};

/** Publishes the engine on window for the classic inline template scripts. */
export function installGlobalMapLayers(): void {
    window.MapLayers = MapLayers;
}

declare global {
    interface Window {
        MapLayers: typeof MapLayers;
    }
}
