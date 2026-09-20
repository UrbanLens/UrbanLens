/**
 * MapLibre-native implementation of the shared layers engine - the counterpart to
 * `map-layers.ts`'s Leaflet one, satisfying the same `MapLayersInstance` contract so
 * `MapLayers.create()` can hand a call site the right engine without the call site knowing which
 * one it built (`D12`'s dual-engine requirement; PL8 item 2). Both drive the same layers strip via
 * `map-layers-panel.ts` and resolve tiles from the same `TILE_DEFS`, so a map draws the same
 * sources on either engine.
 *
 * Two structural differences from the Leaflet engine, both forced by MapLibre rather than chosen:
 *
 * 1. **State lives here, not on the map.** MapLibre rejects `addSource`/`addLayer` until its style
 *    has loaded, so an engine created synchronously after `new maplibregl.Map(...)` has no map to
 *    write to yet. Every toggle therefore mutates plain state below and `applyToMap()` replays it
 *    onto the map once the style is ready - where the Leaflet engine can read its state back off
 *    the map with `hasLayer()` at any time.
 * 2. **Layers are added once and toggled by visibility**, not added to and removed from the map -
 *    the shape REData's own `LayerToggleControl` uses (PL8 item 10).
 */

import { showMapContextMenu } from "./map-context-menu";
import { createLayersPanel } from "./map-layers-panel";
import { normalizeBase, rasterSourceFor, vectorStyleFor } from "./map-layers";
import type { BaseLayerKey, CustomLayerToggle, MapDarkMode, MapLayersInstance, MapLayersOptions, MapLayersState } from "./map-layers";
import { toMapLibreTileUrls } from "./maplibre-raster-style";
import { fetchVectorStyle } from "./maplibre-vector-style";
import type { NamespacedVectorStyle } from "./maplibre-vector-style";

import type { Map as MaplibreMap, MapMouseEvent, RasterSourceSpecification } from "maplibre-gl";

/** Namespaced so a page's own sources/layers can never collide with the engine's. */
const LAYER_PREFIX = "ul-layers-";

/** Namespace for everything a fetched vector style contributes, per base layer key. */
const vectorPrefixFor = (kind: string): string => `${LAYER_PREFIX}vector-${kind}-`;

const BASE_LAYER_IDS = {
    street: `${LAYER_PREFIX}street`,
    dark: `${LAYER_PREFIX}dark`,
    topographic: `${LAYER_PREFIX}topographic`,
    satellite: `${LAYER_PREFIX}satellite`,
} as const;
const BORDERS_LAYER_ID = `${LAYER_PREFIX}borders`;
const RAIN_LAYER_ID = `${LAYER_PREFIX}weather-rain`;
const CLOUDS_LAYER_ID = `${LAYER_PREFIX}weather-clouds`;

const OPENWEATHER_ATTRIBUTION = 'Map data &copy; <a href="https://openweathermap.org">OpenWeatherMap</a>';

/**
 * Exactly the CSS `invert(100%) hue-rotate(180deg) brightness(90%)` the Leaflet engine applies to
 * its topo pane, expressed in the raster paint properties MapLibre has instead (it has no invert).
 *
 * The equivalence is exact, not approximate, and was checked against the pinned
 * `maplibre-gl@5.24.0` bundle's own raster fragment shader rather than the style-spec docs: it ends
 * `mix(vec3(brightness_min), vec3(brightness_max), rgb)`, i.e. `min + rgb * (max - min)`, so
 * `min = 0.9, max = 0` yields `0.9 * (1 - rgb)` - an inversion scaled to 90% brightness. The hue
 * rotation runs *before* that in the shader and *after* the invert in CSS, which cancels out
 * because MapLibre's hue-rotation matrix is luminance-preserving (its rows sum to 1, so it maps
 * white to white): `M(1 - c) = 1 - M(c)`.
 */
const TOPO_DARK_PAINT: Record<string, number> = {
    "raster-hue-rotate": 180,
    "raster-brightness-min": 0.9,
    "raster-brightness-max": 0,
};

/** MapLibre's own raster paint defaults - what `TOPO_DARK_PAINT` is reverted to in light mode. */
const TOPO_LIGHT_PAINT: Record<string, number> = {
    "raster-hue-rotate": 0,
    "raster-brightness-min": 0,
    "raster-brightness-max": 1,
};

/**
 * Whether `map` is a MapLibre map rather than a Leaflet one.
 *
 * Duck-typed on a MapLibre-only method rather than `instanceof maplibregl.Map`, because
 * `maplibregl` is a CDN global that is simply absent on pages that never load it.
 */
export function isMaplibreMap(map: object): map is MaplibreMap {
    return typeof (map as Partial<MaplibreMap>).setLayoutProperty === "function";
}

interface ManagedRasterLayer {
    id: string;
    /** `TILE_DEFS` key this layer resolves its tiles from. */
    kind: string;
    opacity: number;
}

/** Bottom-to-top draw order; `street`/`dark` are the mutually exclusive bottom base, the rest stack above it. */
const MANAGED_LAYERS: ManagedRasterLayer[] = [
    { id: BASE_LAYER_IDS.street, kind: "street", opacity: 1 },
    { id: BASE_LAYER_IDS.dark, kind: "dark", opacity: 1 },
    { id: BASE_LAYER_IDS.topographic, kind: "topographic", opacity: 1 },
    { id: BASE_LAYER_IDS.satellite, kind: "satellite", opacity: 1 },
    // Matches the 0.6 the Leaflet borders overlay already uses (`TILE_DEFS.borders`).
    { id: BORDERS_LAYER_ID, kind: "borders", opacity: 0.6 },
];

/**
 * Creates the MapLibre layers engine for `map`.
 *
 * Reached through `createMapLayers()` (`map-layers.ts`), which dispatches on the engine - call
 * sites should keep using `MapLayers.create()` rather than this directly.
 * @param map - A MapLibre map, whose style may still be loading.
 * @param options - The same `MapLayersOptions` the Leaflet engine takes. `contextMenu`'s
 * `shouldOpen`/`onOpen` callbacks are Leaflet-event-typed and are ignored here; nothing in this app
 * passes them (every call site passes `false` or nothing at all).
 */
export function createMaplibreMapLayers(map: MaplibreMap, options: MapLayersOptions = {}): MapLayersInstance {
    const opts = options;
    const root: HTMLElement | null =
        typeof opts.root === "string" ? document.querySelector<HTMLElement>(opts.root) : (opts.root ?? null);

    let darkMode: MapDarkMode = opts.darkMode || "light";
    const custom: Record<string, CustomLayerToggle> = { ...(opts.custom || {}) };
    const weatherKey = opts.apiKey || null;

    let base: BaseLayerKey = "street";
    let weatherOn = false;
    let bordersOn = false;
    let styleReady = false;
    let destroyed = false;

    /** The base layer whose vector style is on the map, and what it put there, or null for raster. */
    let vectorKind: string | null = null;
    let vectorStyle: NamespacedVectorStyle | null = null;
    /** What the current base *wants*, which lags `vectorKind` while a style document is in flight. */
    let wantedVectorKind: string | null = null;
    let vectorRequest: AbortController | null = null;
    /**
     * Styles that could not be fetched or parsed, so a deployment whose style URL is broken draws
     * its raster fallback once rather than re-asking on every layer toggle.
     */
    const unavailableVectorKinds = new Set<string>();

    const remember = opts.defaultBase === "remember" && !!opts.storageKey;

    (function readInitialState() {
        let requested = opts.defaultBase || "street";
        weatherOn = (opts.initialOverlays || []).includes("weather");
        bordersOn = (opts.initialOverlays || []).includes("borders");
        if (requested === "remember") {
            requested = "street";
            try {
                const saved = JSON.parse(localStorage.getItem(opts.storageKey || "") || "null");
                if (saved) {
                    requested = saved.base || "street";
                    weatherOn = !!saved.weather;
                }
            } catch {
                /* corrupt storage - fall back to street */
            }
        }
        base = normalizeBase(requested);
        // No API key means no weather layers get built at all, so the state must not claim
        // otherwise - the Leaflet engine derives this from the layers it actually added.
        weatherOn = weatherOn && !!weatherKey;
    })();

    // -- Dark map mode -----------------------------------------------------------
    function isDarkActive(): boolean {
        if (darkMode === "dark") return true;
        if (darkMode === "light") return false;
        return window.matchMedia("(prefers-color-scheme: dark)").matches;
    }

    // -- Layer setup ---------------------------------------------------------------
    function rasterSource(kind: string): RasterSourceSpecification {
        const source = rasterSourceFor(kind);
        return {
            type: "raster",
            tiles: toMapLibreTileUrls(source.url, source.subdomains),
            // 256px across every vendor this app uses - see BASE_ERROR_TILE_URL's comment in map-layers.ts.
            tileSize: 256,
            minzoom: source.minZoom,
            maxzoom: source.maxNativeZoom,
            attribution: source.attribution,
        };
    }

    function addManagedLayers(): void {
        // Insert beneath whatever the page has already drawn (markup shapes, pin markers) -
        // MapLibre appends to the top of the style by default, which would bury them under tiles.
        const firstExistingLayerId = map.getStyle().layers?.[0]?.id;
        const add = (id: string, source: RasterSourceSpecification, opacity: number): void => {
            if (map.getLayer(id)) return;
            if (!map.getSource(id)) map.addSource(id, source);
            map.addLayer(
                { id, type: "raster", source: id, layout: { visibility: "none" }, paint: { "raster-opacity": opacity } },
                firstExistingLayerId,
            );
        };

        for (const layer of MANAGED_LAYERS) add(layer.id, rasterSource(layer.kind), layer.opacity);

        if (weatherKey) {
            const weatherSource = (layer: string): RasterSourceSpecification => ({
                type: "raster",
                tiles: [`https://tile.openweathermap.org/map/${layer}/{z}/{x}/{y}.png?appid=${weatherKey}`],
                tileSize: 256,
                attribution: OPENWEATHER_ATTRIBUTION,
            });
            add(RAIN_LAYER_ID, weatherSource("precipitation_new"), 0.7);
            add(CLOUDS_LAYER_ID, weatherSource("clouds_new"), 0.5);
        }
    }

    function setVisible(id: string, visible: boolean): void {
        if (!map.getLayer(id)) return;
        map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
    }

    /** Re-applies the topo layer's dark-mode inversion - the Leaflet engine's pane CSS filter, in paint properties. */
    function applyTopoPaint(): void {
        if (!map.getLayer(BASE_LAYER_IDS.topographic)) return;
        const paint = isDarkActive() && base === "topographic" ? TOPO_DARK_PAINT : TOPO_LIGHT_PAINT;
        for (const [property, value] of Object.entries(paint)) map.setPaintProperty(BASE_LAYER_IDS.topographic, property, value);
    }

    // Exposes the effective map style for SCSS (e.g. #map[data-map-style="dark"]).
    function syncStyleAttribute(): void {
        const target = opts.styleTarget ?? map.getContainer();
        target.dataset.mapStyle = isDarkActive() ? "dark" : "light";
    }

    // -- Vector base layers ------------------------------------------------------------
    /**
     * Which `TILE_DEFS`/`VECTOR_STYLE_DEFS` key is actually drawing the ground right now.
     *
     * `street` and `dark` are one layer the dark-mode toggle chooses between; `topographic` and
     * `satellite` sit above whichever of those is showing, so they win when selected.
     */
    function effectiveBaseKind(): string {
        if (base === "topographic" || base === "satellite") return base;
        return isDarkActive() ? "dark" : "street";
    }

    /** The key whose vector style should be on the map, or null when this deployment serves none for it. */
    function vectorKindWanted(): string | null {
        const kind = effectiveBaseKind();
        if (unavailableVectorKinds.has(kind)) return null;
        return vectorStyleFor(kind) ? kind : null;
    }

    /** Where a vector style's layers go: under everything, including the raster overlays. */
    function bottomLayerId(): string | undefined {
        if (map.getLayer(BASE_LAYER_IDS.street)) return BASE_LAYER_IDS.street;
        return map.getStyle().layers?.[0]?.id;
    }

    function addVectorStyle(style: NamespacedVectorStyle): void {
        // Before the layers that reference them, so a symbol layer never renders a frame with no font.
        if (style.glyphs) map.setGlyphs(style.glyphs);
        for (const sprite of style.sprites) map.addSprite(sprite.id, sprite.url);

        const before = bottomLayerId();
        for (const [id, source] of Object.entries(style.sources)) {
            if (!map.getSource(id)) map.addSource(id, source);
        }
        // Each inserted before the same anchor, so the document's own bottom-to-top order survives.
        for (const layer of style.layers) {
            if (!map.getLayer(layer.id)) map.addLayer(layer, before);
        }
    }

    function removeVectorStyle(): void {
        const style = vectorStyle;
        vectorStyle = null;
        vectorKind = null;
        if (!style) return;
        for (const layer of style.layers) {
            if (map.getLayer(layer.id)) map.removeLayer(layer.id);
        }
        for (const id of Object.keys(style.sources)) {
            if (map.getSource(id)) map.removeSource(id);
        }
        for (const sprite of style.sprites) map.removeSprite(sprite.id);
        if (style.glyphs) map.setGlyphs(null);
    }

    /**
     * Brings the map's vector base into line with what the current selection asks for.
     *
     * Asynchronous because the style document is fetched, so every caller is a fire-and-forget: the
     * raster fallback is already visible while this runs, and a base changed again mid-flight is
     * settled by `wantedVectorKind` rather than by whichever fetch happens to land last.
     */
    async function syncVectorBase(): Promise<void> {
        if (!styleReady || destroyed) return;
        const wanted = vectorKindWanted();
        if (wanted === wantedVectorKind) return;
        wantedVectorKind = wanted;

        vectorRequest?.abort();
        vectorRequest = null;
        if (vectorKind !== null && vectorKind !== wanted) removeVectorStyle();
        if (wanted === null) {
            applyVisibility();
            return;
        }

        const def = vectorStyleFor(wanted);
        if (!def) return;
        const request = new AbortController();
        vectorRequest = request;
        const style = await fetchVectorStyle(vectorPrefixFor(wanted), def.styleUrl, request.signal);
        if (destroyed || wantedVectorKind !== wanted) return;
        vectorRequest = null;
        if (!style) {
            // Falls back to raster for the rest of this page rather than retrying per toggle.
            unavailableVectorKinds.add(wanted);
            wantedVectorKind = null;
            applyVisibility();
            return;
        }
        addVectorStyle(style);
        vectorStyle = style;
        vectorKind = wanted;
        applyVisibility();
        opts.onAttribution?.(attributionText());
    }

    /** The raster half of replaying this engine's state; a no-op until the style can accept layers. */
    function applyVisibility(): void {
        if (!styleReady || destroyed) return;
        // A vector style is a whole basemap, so nothing raster draws under it.
        const onVector = vectorKind !== null;
        const dark = isDarkActive();
        setVisible(BASE_LAYER_IDS.street, !onVector && !dark);
        setVisible(BASE_LAYER_IDS.dark, !onVector && dark);
        setVisible(BASE_LAYER_IDS.topographic, !onVector && base === "topographic");
        setVisible(BASE_LAYER_IDS.satellite, !onVector && base === "satellite");
        setVisible(BORDERS_LAYER_ID, bordersOn);
        setVisible(RAIN_LAYER_ID, weatherOn);
        setVisible(CLOUDS_LAYER_ID, weatherOn);
        applyTopoPaint();
        syncStyleAttribute();
    }

    /** Replays this engine's state onto the map; a no-op until the style can accept layers. */
    function applyToMap(): void {
        applyVisibility();
        void syncVectorBase();
    }

    // -- State ---------------------------------------------------------------------
    function baseKey(): BaseLayerKey {
        return base;
    }

    function getState(): MapLayersState {
        return { base, weather: weatherOn, borders: bordersOn, darkMode };
    }

    /**
     * The combined attribution line, replacing MapLibre's own control on pages that render it
     * elsewhere (the main map's footer, the comment-map dialog's toolbar).
     */
    function attributionText(): string {
        const parts: string[] = [];
        // What is drawing the ground is what has to be credited, and a vector style is served by
        // this deployment rather than by the vendor whose raster layer it replaced.
        const vectorDef = vectorKind ? vectorStyleFor(vectorKind) : null;
        if (vectorDef) parts.push(vectorDef.attribution);
        else if (base === "satellite") parts.push("© Esri");
        else if (base === "topographic") parts.push("© OpenTopoMap");
        // Both street and dark are CARTO-served (see TILE_DEFS) - same attribution either way.
        else parts.push("© OSM · CARTO");
        if (weatherKey && weatherOn) parts.push("© OpenWeatherMap");
        if (bordersOn && base !== "satellite") parts.push("© Esri");
        parts.push("MapLibre");
        return parts.join(" · ");
    }

    function persistState(): void {
        if (remember) {
            try {
                localStorage.setItem(opts.storageKey!, JSON.stringify({ base, weather: weatherOn }));
            } catch {
                /* storage unavailable - ignore */
            }
        }
        opts.onStateChange?.(getState());
    }

    function syncButtons(): void {
        panel.sync({ base, weather: weatherOn, borders: bordersOn, dark: isDarkActive(), custom });
    }

    /** The one path every state change goes through, so nothing can update the map without updating the buttons. */
    function commit(): void {
        applyToMap();
        syncButtons();
        opts.onAttribution?.(attributionText());
        persistState();
    }

    // -- Base / overlay switching ----------------------------------------------------
    function setBase(rawKey: string): void {
        base = normalizeBase(rawKey);
        commit();
    }

    // Button semantics from the main map: street always selects street; topo/satellite toggle back off to street.
    function toggleBase(rawKey: string): void {
        const key = normalizeBase(rawKey);
        setBase(key !== "street" && base === key ? "street" : key);
    }

    function toggleWeather(): void {
        if (!weatherKey) return;
        weatherOn = !weatherOn;
        commit();
    }

    function toggleBorders(): void {
        bordersOn = !bordersOn;
        commit();
    }

    function setOverlay(key: string, on: boolean): void {
        if (key === "weather") {
            if (!weatherKey || weatherOn === on) return;
            toggleWeather();
        } else if (key === "borders") {
            if (bordersOn !== on) toggleBorders();
        }
    }

    function toggleCustom(key: string): void {
        const layer = custom[key];
        if (!layer) return;
        const wasActive = layer.isActive();
        layer.toggle();
        // Turning markups off implies boundaries should go with it.
        if (key === "details" && wasActive) setOverlay("borders", false);
        syncButtons();
    }

    function registerToggle(key: string, toggle: CustomLayerToggle): void {
        custom[key] = toggle;
        syncButtons();
    }

    function setDarkMode(mode: MapDarkMode): void {
        darkMode = mode;
        applyToMap();
        syncButtons();
        opts.onAttribution?.(attributionText());
    }

    function toggleDark(): void {
        const newMode: MapDarkMode = darkMode === "dark" ? "light" : "dark";
        setDarkMode(newMode);
        opts.onDarkModeChange?.(newMode);
        persistState();
    }

    // -- Panel ------------------------------------------------------------------------
    const panel = createLayersPanel(root, !!weatherKey, { toggleBase, toggleWeather, toggleBorders, toggleDark, toggleCustom });

    // -- OS dark-mode preference ---------------------------------------------------------
    let colorSchemeQuery: MediaQueryList | null = null;
    let onColorSchemeChange: (() => void) | null = null;
    if (darkMode === "system") {
        colorSchemeQuery = window.matchMedia("(prefers-color-scheme: dark)");
        onColorSchemeChange = () => {
            applyToMap();
            syncButtons();
        };
        colorSchemeQuery.addEventListener("change", onColorSchemeChange);
    }

    // -- Tile loading visual feedback ------------------------------------------------------
    // "idle" fires once the map has nothing left to load or render, so unlike a per-layer
    // counter it cannot be left stuck by a tile that errors instead of loading.
    let onDataLoading: (() => void) | null = null;
    let onIdle: (() => void) | null = null;
    if (opts.loadingTarget) {
        const target = opts.loadingTarget;
        onDataLoading = () => target.classList.add("tiles-loading");
        onIdle = () => target.classList.remove("tiles-loading");
        map.on("dataloading", onDataLoading);
        map.on("idle", onIdle);
    }

    // -- Right-click menu ---------------------------------------------------------------------
    const contextMenuOptions = typeof opts.contextMenu === "object" ? opts.contextMenu : {};
    let onContextMenu: ((event: MapMouseEvent) => void) | null = null;
    if (opts.contextMenu !== false) {
        onContextMenu = (event: MapMouseEvent): void => {
            const target = event.originalEvent.target as Element | null;
            if (target?.closest?.(".maplibregl-ctrl, .maplibregl-popup, .map-context-menu")) return;
            const { lat, lng } = event.lngLat;
            showMapContextMenu({
                lat,
                lng,
                zoom: map.getZoom(),
                clientX: event.originalEvent.clientX,
                clientY: event.originalEvent.clientY,
                extraItems: contextMenuOptions.extraItems?.(lat, lng) ?? [],
                onClose: contextMenuOptions.onClose,
                streetViewCheckUrl: contextMenuOptions.streetViewCheckUrl,
            });
        };
        map.on("contextmenu", onContextMenu);
    }

    // -- Initial state ---------------------------------------------------------------------------
    // The style is usually still loading when a call site creates this engine one line after
    // `new maplibregl.Map(...)`, but it may already be up if the map was built earlier.
    const onStyleReady = (): void => {
        if (styleReady || destroyed) return;
        styleReady = true;
        addManagedLayers();
        applyToMap();
    };
    map.once("load", onStyleReady);
    if (map.isStyleLoaded()) onStyleReady();
    // Not left to applyToMap(), which no-ops until the style is ready: this only touches the
    // container's dataset, and SCSS should not see an unstyled map while tiles are still loading.
    syncStyleAttribute();
    syncButtons();
    opts.onAttribution?.(attributionText());

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
        openPanel: panel.open,
        closePanel: panel.close,
        togglePanel: panel.toggle,
        isPanelOpen: panel.isOpen,
        syncButtons,
        getState,
        baseKey,
        destroy: () => {
            destroyed = true;
            vectorRequest?.abort();
            vectorRequest = null;
            map.off("load", onStyleReady);
            if (onDataLoading) map.off("dataloading", onDataLoading);
            if (onIdle) map.off("idle", onIdle);
            if (onContextMenu) map.off("contextmenu", onContextMenu);
            if (colorSchemeQuery && onColorSchemeChange) colorSchemeQuery.removeEventListener("change", onColorSchemeChange);
            panel.destroy();
        },
    };
}
