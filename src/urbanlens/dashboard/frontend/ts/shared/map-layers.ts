/**
 * Shared map layers component - the single source of truth for every Leaflet map on the site.
 */

// Leaflet is loaded via a CDN <script> tag on map pages, so it must be typed as an ambient global rather than imported.
declare const L: typeof import("leaflet");

// Type only - this module never touches the MapLibre runtime, it just hands a MapLibre map to the engine in `maplibre-layers.ts`.
import type { Map as MaplibreMap } from "maplibre-gl";

import { bindMapContextMenu, type BindMapContextMenuOptions } from "./map-context-menu";
import { createLayersPanel } from "./map-layers-panel";
import { createMaplibreMapLayers, isMaplibreMap } from "./maplibre-layers";
import type { RasterSourceInput } from "./maplibre-raster-style";
import { acquireOwnTileSlot, isOwnTileUrl, ownTileRetriesAreSuspended, ownTileRetryDelayMs, recordOwnTileOutcome } from "./own-tiles";

export type BaseLayerKey = "street" | "topographic" | "satellite";
export type MapDarkMode = "light" | "dark" | "system";

/**
 * What a map opens on when nothing tells it otherwise - the same value `Profile.default_map_view`
 * defaults to, so a page that never received the viewer's setting still lands where the setting
 * would have put it.
 */
export const DEFAULT_BASE_LAYER: BaseLayerKey = "satellite";

interface TileDef {
    url: string;
    options: L.TileLayerOptions;
}

/**
 * Zoom range every map on the site uses.
 */
export const MAP_MAX_ZOOM = 21;
export const MAP_MIN_ZOOM = 2;

/**
 * The grey a failed base tile is replaced with. Shared with the MapLibre engine
 * (`maplibre-layers.ts`), which has no `errorTileUrl` equivalent and instead paints a background
 * layer this colour beneath its managed raster layers - one literal so the two engines cannot draw
 * a different placeholder.
 */
export const BASE_ERROR_TILE_COLOR = "#999";

/**
 * Shown in place of a base tile that failed to load - a burst of requests on
 * zoom-out (a new zoom level's worth of tiles, all uncached) occasionally
 * draws a 403/5xx from these free vendor CDNs, most likely rate-limiting
 * triggered by this site's blanket `Referrer-Policy: no-referrer` (nginx
 * `django.conf`), which some of them treat as a bot signal. That policy is
 * deliberate - map page URLs can encode a pin's coordinates, and leaking
 * those to a third-party tile vendor via Referer is exactly what a site for
 * "sharing urbex locations responsibly" must not do - so this only smooths
 * the vendor's own occasional failure over, rather than relaxing it. 256px
 * to match every vendor's own tile size here.
 */
const BASE_ERROR_TILE_URL =
    `data:image/svg+xml;charset=UTF-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='256' height='256'%3E%3Crect width='256' height='256' fill='${encodeURIComponent(BASE_ERROR_TILE_COLOR)}'/%3E%3C/svg%3E`;

/** Shown in place of a failed *overlay* tile - transparent, so a flaky boundary/weather tile leaves the base map showing through instead of painting a grey patch over it. */
const OVERLAY_ERROR_TILE_URL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==";

/**
 * Canonical tile sources. maxNativeZoom caps tile requests at each provider's
 * real depth while maxZoom lets Leaflet upscale beyond it (Google-like) so a
 * layer never drops out when the user zooms past the native depth.
 */
const TILE_DEFS: Record<string, TileDef> = {
    street: {
        // Not OSM's own tile.openstreetmap.org: that server enforces a usage
        // policy against unauthorized production hotlinking (osm.wiki/Blocked)
        // and answers a violation with a rendered "Access blocked" tile at a
        // 200 status rather than a real error, so it isn't even caught by
        // errorTileUrl below. CARTO's raster CDN serves the same OSM data
        // under terms that permit this, same as "dark" already does.
        url: "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        options: {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
            maxNativeZoom: 20,
            maxZoom: MAP_MAX_ZOOM,
            errorTileUrl: BASE_ERROR_TILE_URL,
        },
    },
    dark: {
        url: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        options: {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
            maxNativeZoom: 20,
            maxZoom: MAP_MAX_ZOOM,
            errorTileUrl: BASE_ERROR_TILE_URL,
        },
    },
    topographic: {
        url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
        options: {
            attribution: "Esri, HERE, Garmin, Intermap, USGS, NPS, &copy; OpenStreetMap contributors, and the GIS User Community",
            maxNativeZoom: 19,
            maxZoom: MAP_MAX_ZOOM,
            errorTileUrl: BASE_ERROR_TILE_URL,
        },
    },
    satellite: {
        url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        options: {
            attribution: "Tiles &copy; Esri &mdash; Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, Getmapping, Aerogrid, IGN, IGP, UPR-EGP, and the GIS User Community",
            maxNativeZoom: 19,
            maxZoom: MAP_MAX_ZOOM,
            errorTileUrl: BASE_ERROR_TILE_URL,
        },
    },
    borders: {
        url: "https://services.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
        options: {
            attribution: "Boundaries &copy; Esri",
            maxNativeZoom: 19,
            maxZoom: MAP_MAX_ZOOM,
            opacity: 0.6,
            pane: "overlayPane",
            errorTileUrl: OVERLAY_ERROR_TILE_URL,
        },
    },
};

/**
 * Snapshot of the built-in sources above, taken before anything can register over them, so
 * `resetRedataLayersCacheForTests` can put `TILE_DEFS` back and `registerCatalogue` can keep each
 * layer's own presentation when it swaps in this deployment's URL. Shallow by design: both read
 * an entry's `options`, neither mutates one.
 */
const BUILT_IN_TILE_DEFS: Record<string, TileDef> = { ...TILE_DEFS };

/**
 * A base layer this deployment serves as a MapLibre style document rather than an XYZ raster
 * template (`D11`) - the self-hosted shape, where the browser fetches the style and the vector
 * tiles it names directly and REData proxies neither.
 */
export interface VectorStyleDef {
    styleUrl: string;
    attribution: string;
    minZoom: number;
    maxZoom: number;
}

/**
 * Vector sources registered from the catalogue, by the same key `TILE_DEFS` uses, so a layer
 * offered in both shapes resolves to one or the other by engine rather than by id.
 *
 * Never populated with a built-in: there is no vendor default here. An empty entry means this
 * deployment has no self-hosted style for that layer and both engines fall back to `TILE_DEFS`.
 */
const VECTOR_STYLE_DEFS: Record<string, VectorStyleDef> = {};

/**
 * The self-hosted style document for one of the canonical sources, if this deployment serves one.
 *
 * Only `maplibre-layers.ts` can act on the result - Leaflet has no vector renderer, so a Leaflet
 * map draws `TILE_DEFS` for the same key and the two engines diverge on bytes while agreeing on
 * which layer is showing. Both sides are this deployment's own since `D15`; before it, the raster
 * side of a self-hosted layer was a hardcoded vendor CDN.
 * @param kind - Canonical or legacy source key.
 */
export function vectorStyleFor(kind: string): VectorStyleDef | null {
    applyEmbeddedCatalogue();
    return VECTOR_STYLE_DEFS[kind] ?? VECTOR_STYLE_DEFS[normalizeBase(kind)] ?? null;
}

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
 *
 * @param fallback - What an unrecognized identifier means. Callers resolving a *map's opening base*
 * pass {@link DEFAULT_BASE_LAYER}; the tile-def and style lookups keep "street", which is the shape
 * their own callers already handle and mirrors Python's `normalize_layer_mode`.
 */
export function normalizeBase(key: string | null | undefined, fallback: BaseLayerKey = "street"): BaseLayerKey {
    return BASE_ALIASES[(key || "").toLowerCase()] || fallback;
}

/** The canonical bases a panel actually offers a button for, in the order the page listed them. */
function offeredBases(root: HTMLElement | null): BaseLayerKey[] {
    const found: BaseLayerKey[] = [];
    for (const button of root?.querySelectorAll<HTMLElement>('[data-layer-kind="base"]') ?? []) {
        const key = BASE_ALIASES[(button.dataset.mapLayer || "").toLowerCase()];
        if (key && !found.includes(key)) found.push(key);
    }
    return found;
}

/**
 * Which base a map opens on, given what the call site asked for and what the panel says.
 *
 * The panel root carries the viewer's configured base, so a call site that names none still honours
 * the setting. Each page also names the bases it offers, and a base with no button on this page
 * would strand the viewer on a layer they cannot switch away from - so it resolves to one they can.
 *
 * "remember" is returned untouched: only the caller knows whether it has somewhere to remember.
 */
export function resolveConfiguredBase(root: HTMLElement | null, requested?: string | null): string {
    const configured = requested || root?.dataset.defaultBase || "";
    if (configured === "remember") return configured;

    // Only a map nobody named a base for takes the constant. A named one keeps the meaning it
    // already has - "dark" is a stored `MapLayerMode` that `BASE_ALIASES` deliberately reads as the
    // street base with dark mode on, not an unrecognized value.
    const key = configured ? normalizeBase(configured) : DEFAULT_BASE_LAYER;
    const offered = offeredBases(root);
    if (!offered.length || offered.includes(key)) return key;
    return offered.includes(DEFAULT_BASE_LAYER) ? DEFAULT_BASE_LAYER : offered[0]!;
}


/**
 * Creates a tile layer for one of the canonical sources.
 * @param kind - Canonical or legacy source key ("street", "standard", "satellite", "topo", "dark", ...).
 * @param extraOptions - Leaflet options merged over the canonical defaults (e.g. pane).
 */
export function tileLayer(kind: string, extraOptions?: L.TileLayerOptions): L.TileLayer {
    applyEmbeddedCatalogue();
    const def = TILE_DEFS[kind] || TILE_DEFS[normalizeBase(kind)] || TILE_DEFS.street!;
    const options = { ...def.options, ...extraOptions };
    // A vendor's tiles are asked for the way Leaflet always has. This deployment's own go through
    // the queue in `own-tiles.ts`, because the proxy serving them can only fetch a few at a time.
    return isOwnTileUrl(def.url) ? new (ownTileLayerClass())(def.url, options) : L.tileLayer(def.url, options);
}

// Both are CDN globals, loaded only by pages that ask for the vector base (`maplibregl_js` and
// `maplibregl_leaflet_js`), so neither can be imported.
declare const maplibregl: typeof import("maplibre-gl") | undefined;

declare module "leaflet" {
    /** `@maplibre/maplibre-gl-leaflet`: draws a whole MapLibre style as one Leaflet layer. */
    function maplibreGL(options: { style: string; attribution?: string }): L.Layer;
}

/**
 * Whether this page can draw a vector base inside its Leaflet map.
 *
 * Read per call rather than latched at import: `core.js` loads on every page, and the two globals
 * arrive from `<script>` tags only the map pages carry.
 */
function canDrawVectorBase(): boolean {
    return typeof maplibregl !== "undefined" && typeof L.maplibreGL === "function";
}

/**
 * The base layer for one of the canonical sources, vector where this deployment offers one.
 *
 * A vector base is fetched by the browser straight from the style's own CDN, so it costs this
 * origin nothing and is not subject to the proxy's upstream slots (`own-tiles.ts`). The raster
 * return is the fallback for a page that loaded neither global, and for every layer the catalogue
 * publishes without a `style_url`.
 * @param kind - Canonical or legacy source key.
 * @param extraOptions - Leaflet options for the raster fallback; a vector base takes none.
 */
export function baseLayer(kind: string, extraOptions?: L.TileLayerOptions): L.Layer {
    const def = vectorStyleFor(kind);
    if (def && canDrawVectorBase()) return L.maplibreGL({ style: def.styleUrl, attribution: def.attribution });
    return tileLayer(kind, extraOptions);
}

type OwnTileLayerClass = new (url: string, options?: L.TileLayerOptions) => L.TileLayer;

/**
 * Built on first use rather than at import: this module is bundled into `core.js`, which every page
 * loads, and `L` is a CDN global that only map pages have. Keyed on what it was derived from, so
 * there is no stale subclass to reset when something swaps Leaflet out underneath it.
 */
let ownTileLayerCache: { base: unknown; cls: OwnTileLayerClass } | null = null;

function ownTileLayerClass(): OwnTileLayerClass {
    if (!ownTileLayerCache || ownTileLayerCache.base !== L.TileLayer) {
        ownTileLayerCache = { base: L.TileLayer, cls: L.TileLayer.extend(OWN_TILE_LAYER) };
    }
    return ownTileLayerCache.cls;
}

/**
 * A `TileLayer` that queues its requests and asks again when the proxy says it is busy.
 *
 * Leaflet asks for a whole viewport at once and treats a failed tile as finished - it paints
 * `errorTileUrl` and never comes back - so against a proxy with a handful of upstream slots, stock
 * behaviour leaves most of a cold viewport permanently holed. Queueing is what fixes that; the
 * retry only covers the slots this page does not control (see `own-tiles.ts`).
 *
 * Subclassed rather than patched onto an instance because `createTile` is Leaflet's documented
 * extension point for exactly this, and the alternative reaches past `protected`.
 */
/**
 * How to finish a tile of one of these layers, keyed by the element Leaflet holds.
 *
 * Leaflet drops a tile by overwriting its `onload` and `onerror` with a no-op of its own and, when
 * the element is incomplete, removing it - `_abortLoading` does that to every tile off the new zoom
 * and `_removeTile` to every one pruned. Neither handler fires again, so a tile dropped mid-request
 * cannot notice on its own: it keeps its slot, and Leaflet keeps counting it as outstanding.
 */
const ownTileSlotHolders = new WeakMap<HTMLElement, () => void>();

/**
 * Finishes a tile when Leaflet says it is no longer wanted.
 *
 * Once per layer rather than per tile: these fire on the layer, and it is the element that says
 * which request they are about.
 * @param layer - The layer to listen to.
 */
function finishTilesLeafletAbandons(layer: L.TileLayer): void {
    const wired = layer as L.TileLayer & { _ownTileSlotsWired?: boolean };
    if (wired._ownTileSlotsWired) return;
    wired._ownTileSlotsWired = true;
    const handBack = (event: L.TileEvent): void => ownTileSlotHolders.get(event.tile)?.();
    layer.on("tileunload", handBack);
    layer.on("tileabort", handBack);
}

const OWN_TILE_LAYER = {
    createTile(this: L.TileLayer, coords: L.Coords, done: L.DoneCallback): HTMLElement {
        finishTilesLeafletAbandons(this);
        const tile = document.createElement("img");
        tile.alt = "";
        const options = this.options;
        // Both mirror Leaflet's own createTile, including its "only when string" note on referrerPolicy.
        if (options.crossOrigin || options.crossOrigin === "") tile.crossOrigin = options.crossOrigin === true ? "" : options.crossOrigin;
        if (typeof options.referrerPolicy === "string") tile.referrerPolicy = options.referrerPolicy;

        // Resolved now, while `coords` and the layer's zoom still agree: getTileUrl() fills {z}
        // from the layer's *current* zoom rather than from the coords it is handed, so asking for
        // it again after a wait would paint a tile of somewhere else into this one.
        const url = this.getTileUrl(coords);
        let attempt = 0;
        let finished = false;
        let releaseSlot: (() => void) | null = null;
        const release = (): void => {
            releaseSlot?.();
            releaseSlot = null;
        };

        const finish = (error?: Error): void => {
            if (finished) return;
            finished = true;
            release();
            // Before the placeholder is painted: assigning it is a load like any other, so a tile
            // still listening would report a success for the picture of its own failure.
            tile.onload = null;
            tile.onerror = null;
            // Always answered, even for a tile nobody is waiting for any more: Leaflet counts
            // outstanding tiles to decide when a layer has finished loading, and one that never
            // reports leaves the map's loading indicator on forever.
            if (error && options.errorTileUrl && tile.getAttribute("src") !== options.errorTileUrl) tile.src = options.errorTileUrl;
            done(error, tile);
        };

        const request = (): void => {
            if (finished) return;
            if (attempt > 0) {
                // Panned or zoomed away while queued - the slot is worth more to a tile still on screen.
                if (!tile.isConnected || !stillOurs()) {
                    finish(new Error("tile no longer needed"));
                    return;
                }
                // Asked again when the wait is over rather than when it was scheduled, so a retry
                // queued before the deployment stopped answering is not still spent afterwards.
                if (ownTileRetriesAreSuspended()) {
                    finish(new Error(`Tile ${url} not retried while the deployment is refusing tiles`));
                    return;
                }
            }
            void acquireOwnTileSlot().then((releaser) => {
                // The queue can hand a slot over long after this tile gave up or was dropped.
                // Kept, it narrows the queue for every tile still trying; used, it spends a slot
                // and a request on a zoom the map has already left.
                if (finished) {
                    releaser();
                    return;
                }
                releaseSlot = releaser;
                if (!stillOurs()) {
                    finish();
                    return;
                }
                tile.src = url;
            });
        };

        const onLoad = (): void => {
            recordOwnTileOutcome(true);
            finish();
        };
        const onError = (): void => {
            release();
            // An <img> error carries no status, so a hole in the layer is counted here the same as a
            // refusal. Both mean asking again is unlikely to help, and any tile that does load
            // clears it - which is also the only thing this affects, since a first attempt is
            // always made.
            recordOwnTileOutcome(false);
            const delay = ownTileRetriesAreSuspended() ? null : ownTileRetryDelayMs(attempt);
            if (delay === null) {
                finish(new Error(`Tile ${url} failed after ${attempt + 1} attempts`));
                return;
            }
            attempt++;
            setTimeout(request, delay);
        };

        tile.onload = onLoad;
        tile.onerror = onError;
        // An `<img>` with no `src` reports `complete`, so `_abortLoading` leaves a queued tile in
        // place rather than removing it, and fires nothing. Whether the handlers are still the ones
        // set above is the one signal both paths share.
        const stillOurs = (): boolean => tile.onload === onLoad;
        ownTileSlotHolders.set(tile, () => finish());

        request();
        return tile;
    },
};

/** The named entities a tile attribution actually uses, plus the ones any HTML may carry. */
const ATTRIBUTION_ENTITIES: Record<string, string> = {
    amp: "&",
    copy: "©",
    gt: ">",
    lt: "<",
    mdash: "—",
    nbsp: " ",
    ndash: "–",
    quot: '"',
    reg: "®",
    trade: "™",
};

/**
 * A `TILE_DEFS` attribution as the credit line can show it.
 *
 * Those strings are written for Leaflet's own attribution control, which renders them as HTML -
 * links and `&copy;` and all. `setAttribution` writes the credit line with `textContent`, so
 * anything left as markup is shown to the reader verbatim.
 * @param html - The def's attribution, or a REData catalogue entry's.
 */
export function attributionAsText(html: string): string {
    return html
        .replace(/<[^>]*>/g, "")
        .replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (whole, ref: string) => {
            if (ref.startsWith("#x") || ref.startsWith("#X")) return String.fromCodePoint(parseInt(ref.slice(2), 16));
            if (ref.startsWith("#")) return String.fromCodePoint(Number(ref.slice(1)));
            return ATTRIBUTION_ENTITIES[ref.toLowerCase()] ?? whole;
        })
        .replace(/\s+/g, " ")
        .trim();
}

/**
 * Resolves one of the canonical sources to the shape `buildRasterStyle` (`maplibre-raster-style.ts`)
 * needs - the MapLibre-side counterpart to `tileLayer()` above. Same resolution order (`kind` as
 * given, then its normalized base key, then `street`), so a MapLibre and a Leaflet map built from the
 * same `kind` string draw the same tiles.
 */
export function rasterSourceFor(kind: string): RasterSourceInput {
    applyEmbeddedCatalogue();
    const def = TILE_DEFS[kind] || TILE_DEFS[normalizeBase(kind)] || TILE_DEFS.street!;
    return {
        url: def.url,
        attribution: typeof def.options.attribution === "string" ? def.options.attribution : undefined,
        // Only a REData-registered entry currently carries a minZoom; dropping it would let a
        // MapLibre map request tiles below the depth the catalogue says that layer serves.
        minZoom: def.options.minZoom,
        maxNativeZoom: def.options.maxNativeZoom,
        subdomains: def.options.subdomains,
        opacity: def.options.opacity,
    };
}

/**
 * REData's layer ids that name a different key than the canonical `TILE_DEFS`/`BaseLayerKey`
 * entry a map actually looks up (mirrors `BASE_ALIASES`, which normalizes the reverse direction
 * for user-facing input). REData calls the topographic base layer "terrain"; this site calls it
 * "topographic" everywhere `tileLayer()` is called from `createMapLayers()`.
 */
const REDATA_ID_ALIASES: Record<string, string> = {
    terrain: "topographic",
};

let redataLayersPromise: Promise<string[]> | null = null;

/** `<script type="application/json">` written by `{% basemap_tile_catalogue %}` in `themes/base.html`. */
const EMBEDDED_CATALOGUE_ID = "ul-basemap-tiles";

/** Whether the embedded catalogue has been looked for yet (it is read once, on first use). */
let embeddedCatalogueRead = false;

/** What the embed registered, or `null` when this page carried none. */
let embeddedRegisteredIds: string[] | null = null;

/**
 * Reads the catalogue `themes/base.html` embedded in this document and registers it, once.
 *
 * Every entry point into this module that resolves a tile source calls this first, so the
 * registration lands before the first tile request rather than one network round trip after it.
 * A page rendered outside that base template (an htmx fragment, a bare test harness) has no
 * element here, which is what leaves `registerRedataLayers()` a reason to exist.
 * @returns Whether an embedded catalogue was present - `true` even when it registered nothing,
 * since the server saying "no extra layers" is an answer, not a missing one.
 */
function applyEmbeddedCatalogue(): boolean {
    if (!embeddedCatalogueRead) {
        embeddedCatalogueRead = true;
        // Guarded rather than read at module scope: this module is imported by bundles that run
        // before the DOM exists, and by tests with no document at all.
        if (typeof document === "undefined") return false;
        const el = document.getElementById(EMBEDDED_CATALOGUE_ID);
        if (!el?.textContent) return false;
        let layers: RedataLayer[];
        try {
            layers = JSON.parse(el.textContent) as RedataLayer[];
        } catch {
            return false;
        }
        embeddedRegisteredIds = registerCatalogue(layers);
    }
    return embeddedRegisteredIds !== null;
}

/**
 * Registers each catalogue entry into `TILE_DEFS` so `tileLayer()` resolves it by id like any
 * other source, replacing the built-in vendor URL for that id.
 *
 * Vector entries (`source_type: "vector"`) also carry a `style_url`, which lands in
 * `VECTOR_STYLE_DEFS` - `TILE_DEFS` describes a raster template and Leaflet can only draw one of
 * those. Since REData's `D15` the two are not exclusive, so such an entry registers in both and the
 * engine picks: MapLibre draws the style, Leaflet the proxied template. See `maplibre-layers.ts`.
 * @returns The ids registered, in catalogue order.
 */
function registerCatalogue(layers: RedataLayer[]): string[] {
    const registered: string[] = [];
    for (const layer of layers) {
        if (!layer.id || !layer.attribution) continue;
        const key = REDATA_ID_ALIASES[layer.id] ?? layer.id;
        // Absent source_type means a pre-D11 REData deployment, which only ever served raster.
        const isVector = layer.source_type === "vector" && !!layer.style_url;
        if (isVector) {
            VECTOR_STYLE_DEFS[key] = {
                styleUrl: layer.style_url!,
                attribution: layer.attribution,
                minZoom: layer.min_zoom ?? 0,
                maxZoom: layer.max_zoom ?? MAP_MAX_ZOOM,
            };
        }
        // Registered alongside the style rather than instead of it. A vector entry's raster half is
        // a different dataset with its own credit and depth, so those come from the `fallback_*`
        // fields; without this the key keeps its built-in vendor URL and every Leaflet map goes on
        // hotlinking a public CDN, which is the dependency this whole arrangement exists to end.
        if (layer.url_template) {
            TILE_DEFS[key] = {
                url: layer.url_template,
                options: {
                    // The catalogue says where a layer's tiles come from, not how this site draws it.
                    // `borders` is an overlay - its pane, its 0.6 opacity and its *transparent* error
                    // placeholder are this site's, and replacing the whole def dropped all three, so a
                    // REData `borders` layer painted at full opacity in the base layer's own pane.
                    errorTileUrl: BASE_ERROR_TILE_URL,
                    ...BUILT_IN_TILE_DEFS[key]?.options,
                    attribution: (isVector ? layer.fallback_attribution : undefined) ?? layer.attribution,
                    // Leaflet upscales past the vendor's real depth rather than
                    // dropping the layer out, matching the built-in defs above.
                    maxNativeZoom: (isVector ? layer.fallback_max_zoom : undefined) ?? layer.max_zoom ?? 19,
                    maxZoom: MAP_MAX_ZOOM,
                    minZoom: (isVector ? layer.fallback_min_zoom : undefined) ?? layer.min_zoom ?? 0,
                },
            };
        }
        if (isVector || layer.url_template) registered.push(key);
    }
    return registered;
}

/**
 * Ensures this deployment's REData tile catalogue is registered.
 *
 * Resolves without a request when `themes/base.html` already embedded it, which is every page
 * built on that template. The fetch is the fallback for a client rendered without the embed.
 *
 * Memoized: every caller awaits the same in-flight/resolved fetch, so registering before
 * constructing a map's layers costs one request per page load, not one per map.
 * @returns The ids registered, in catalogue order - empty when REData offers none, is
 * unconfigured, or unreachable.
 */
export function registerRedataLayers(): Promise<string[]> {
    if (applyEmbeddedCatalogue()) return Promise.resolve(embeddedRegisteredIds ?? []);
    redataLayersPromise ??= fetchAndRegisterRedataLayers();
    return redataLayersPromise;
}

/**
 * Clears the memoized fetch so the next `registerRedataLayers()` call issues a fresh request, and
 * restores `TILE_DEFS` to its built-in state. Test-only.
 *
 * The restore matters beyond the test that registered: `TILE_DEFS` is module-global and
 * `registerRedataLayers` overwrites built-in entries in place (a catalogue layer id `terrain`
 * lands on `topographic`, per `REDATA_ID_ALIASES`), so without it a registration leaks into every
 * later test in the same process - which is how it was found.
 */
export function resetRedataLayersCacheForTests(): void {
    redataLayersPromise = null;
    embeddedCatalogueRead = false;
    embeddedRegisteredIds = null;
    for (const key of Object.keys(TILE_DEFS)) {
        if (!(key in BUILT_IN_TILE_DEFS)) delete TILE_DEFS[key];
    }
    Object.assign(TILE_DEFS, BUILT_IN_TILE_DEFS);
    for (const key of Object.keys(VECTOR_STYLE_DEFS)) delete VECTOR_STYLE_DEFS[key];
}

async function fetchAndRegisterRedataLayers(): Promise<string[]> {
    let layers: RedataLayer[];
    try {
        const response = await fetch("/dashboard/map/basemap-tiles/sources/", { headers: { Accept: "application/json" } });
        if (!response.ok) return [];
        layers = ((await response.json()) as { layers?: RedataLayer[] }).layers || [];
    } catch {
        return [];
    }
    return registerCatalogue(layers);
}

interface RedataLayer {
    id: string;
    name: string;
    /** Absent on a REData deployment that predates the `D11` contract - treat as raster. */
    source_type?: "raster" | "vector";
    attribution: string;
    url_template?: string;
    style_url?: string;
    min_zoom?: number | null;
    max_zoom?: number | null;
    /**
     * The raster half of a vector entry, which is a different dataset from the style's - its own
     * credit and its own depth. Absent on a REData deployment that predates `D15`.
     */
    fallback_attribution?: string;
    fallback_min_zoom?: number | null;
    fallback_max_zoom?: number | null;
}

/** Creates the geopolitical borders overlay (same tiles on every map). */
export function bordersOverlay(): L.TileLayer {
    return tileLayer("borders");
}

/** Creates the OpenWeatherMap rain + clouds overlay pair. */
export function weatherLayers(apiKey: string): { rain: L.TileLayer; clouds: L.TileLayer } {
    const attribution = 'Map data &copy; <a href="https://openweathermap.org">OpenWeatherMap</a>';
    const make = (layer: string, opacity: number) =>
        L.tileLayer(`https://tile.openweathermap.org/map/${layer}/{z}/{x}/{y}.png?appid=${apiKey}`, {
            attribution,
            opacity,
            maxZoom: MAP_MAX_ZOOM,
            errorTileUrl: OVERLAY_ERROR_TILE_URL,
        });
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
    /**
 * Right-click menu.
 */
    contextMenu?: boolean | BindMapContextMenuOptions;
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
    /** Releases every listener this instance registered outside the map itself (document, matchMedia, context menu) - call when tearing down a per-dialog/per-panel map that outlives a single page load. */
    destroy: () => void;
}

/**
 * Creates the layers engine for a map and binds it to the rendered panel.
 *
 * Dispatches on which rendering engine actually holds the map, so a call site
 * gets the right engine without knowing which one it built (`D12`'s dual-engine
 * requirement - see PL8 item 2).
 * @param map - The map instance, Leaflet or MapLibre.
 * @param options - Behavior configuration; see MapLayersOptions.
 * @returns The engine instance driving both the layers and the panel buttons.
 */
export function createMapLayers(map: L.Map | MaplibreMap, options: MapLayersOptions = {}): MapLayersInstance {
    // Before either engine reads a source: this deployment's own layers must be registered ahead
    // of the first tile request, not one round trip after it.
    applyEmbeddedCatalogue();
    if (isMaplibreMap(map)) return createMaplibreMapLayers(map, options);
    return createLeafletMapLayers(map, options);
}

function createLeafletMapLayers(map: L.Map, options: MapLayersOptions = {}): MapLayersInstance {
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
    const streetLayer = baseLayer("street");
    const darkLayer = baseLayer("dark");
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

    // -- Loading underlay -------------------------------------------------------
    // A gap in the tile grid shows the container's own flat colour, which is whatever CSS picked
    // rather than anything about the place being drawn - and a fast zoom opens gaps faster than
    // tiles can fill them, because there is no loaded level left to scale from. This draws the
    // same base at world zooms underneath everything and blurs it, so a gap shows roughly the
    // colours of wherever the viewer is and the real tiles read as a sharpening rather than an
    // arrival.
    const underlayPane = "ul-underlay";
    // How far below the map's own zoom the underlay draws. Deep enough that a viewport is a tile
    // or two and the blur has something to work with, shallow enough that each tile still paints.
    const UNDERLAY_LEVELS_COARSER = 3;
    if (!map.getPane(underlayPane)) {
        // Below Leaflet's own tilePane, which is 200.
        map.createPane(underlayPane).style.zIndex = "180";
    }
    const underlays = new Map<string, L.TileLayer>();

    function syncUnderlay(): void {
        const key = map.hasLayer(satelliteLayer)
            ? "satellite"
            : map.hasLayer(topographicLayer)
              ? "topographic"
              : isDarkActive()
                ? "dark"
                : "street";
        for (const [other, layer] of underlays) {
            if (other !== key && map.hasLayer(layer)) map.removeLayer(layer);
        }
        let layer = underlays.get(key);
        if (!layer) {
            // `tileSize` with a matching negative `zoomOffset` is how Leaflet draws a coarser level
            // at the right geography: it asks for tiles UNDERLAY_LEVELS_COARSER levels up and
            // paints each at that many doublings of 256px. A fixed `maxNativeZoom` cannot do this
            // job - it would hold one world tile and ask the browser to paint it at 256 * 2^15 px
            // once the viewer zoomed in, which is past what anything will render, so the underlay
            // silently disappeared exactly where the gaps are worst.
            //
            // A viewport is one or two of these, and they are the same tiles the base itself uses
            // at that zoom, so they come from the cache that already holds them. Never the metered
            // vector base: `tileLayer` is the raster shape. No attribution either - these are the
            // active base's own bytes, credited already by the layer drawing over them.
            layer = tileLayer(key, {
                pane: underlayPane,
                tileSize: 256 * 2 ** UNDERLAY_LEVELS_COARSER,
                zoomOffset: -UNDERLAY_LEVELS_COARSER,
                minNativeZoom: 0,
                attribution: "",
            });
            underlays.set(key, layer);
        }
        if (!map.hasLayer(layer)) layer.addTo(map);
    }

    // Swap between streetLayer and darkLayer without touching satellite/topo.
    // street-or-dark is the bottom base; topo/satellite sit on top.
    function syncBaseLayer(): void {
        // Both of these draw opaque JPEG tiles over the whole viewport, so a base underneath is
        // fetched and then covered - and where `street`/`dark` resolve to a metered vector style,
        // that is quota spent per pan and zoom on tiles nobody can see.
        const hidden = map.hasLayer(satelliteLayer) || map.hasLayer(topographicLayer);
        const wanted = hidden ? null : isDarkActive() ? darkLayer : streetLayer;
        for (const layer of [streetLayer, darkLayer]) {
            if (layer !== wanted && map.hasLayer(layer)) map.removeLayer(layer);
        }
        if (wanted && !map.hasLayer(wanted)) wanted.addTo(map);
        applyTopoFilter();
        syncStyleAttribute();
        syncUnderlay();
    }

    // Re-apply the topo filter when the topo layer itself is toggled.
    const onTopoLayerChange = (e: L.LayerEvent): void => {
        if (e.layer === topographicLayer) applyTopoFilter();
    };
    map.on("layeradd layerremove", onTopoLayerChange);

    // In system mode, re-sync whenever the OS preference changes.
    let colorSchemeQuery: MediaQueryList | null = null;
    let onColorSchemeChange: (() => void) | null = null;
    if (darkMode === "system") {
        colorSchemeQuery = window.matchMedia("(prefers-color-scheme: dark)");
        onColorSchemeChange = () => {
            syncBaseLayer();
            syncButtons();
        };
        colorSchemeQuery.addEventListener("change", onColorSchemeChange);
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

    const configuredBase = resolveConfiguredBase(root, opts.defaultBase);
    const remember = configuredBase === "remember" && !!opts.storageKey;

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
    // Replaces Leaflet's on-map control on pages that render attribution elsewhere (e.g. the main map's footer).
    function attributionText(): string {
        const parts: string[] = [];
        // Read off the def actually drawn rather than named here, because the catalogue replaces
        // these defs at runtime and a self-hosted layer's raster half is a different dataset from
        // the vendor default it displaces. A hardcoded credit would keep naming the old one.
        const creditFor = (key: string, fallback: string): string => {
            // Mirrors `baseLayer()`'s own choice, so the credit names the dataset actually drawn:
            // a vector base and the raster it displaces are different datasets from different vendors.
            const vector = canDrawVectorBase() ? vectorStyleFor(key) : null;
            const credit = vector?.attribution ?? (TILE_DEFS[key]?.options?.attribution as string | undefined);
            return credit ? attributionAsText(credit) : fallback;
        };
        if (map.hasLayer(satelliteLayer)) {
            parts.push(creditFor("satellite", "© Esri"));
        } else if (map.hasLayer(topographicLayer)) {
            parts.push(creditFor("topographic", "© Esri"));
        } else {
            parts.push(creditFor(isDarkActive() ? "dark" : "street", "© OpenStreetMap"));
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

    // A vector overlay can add/remove thousands of paths in one turn.
    let attributionFrame: number | null = null;
    const onAttributionLayerChange = (): void => {
        if (attributionFrame !== null) return;
        attributionFrame = window.requestAnimationFrame(() => {
            attributionFrame = null;
            opts.onAttribution!(attributionText());
        });
    };
    if (opts.onAttribution) {
        map.on("layeradd layerremove", onAttributionLayerChange);
    }

    // -- Tile loading visual feedback -------------------------------------------------
    // Grey-dim the target while base tiles download; restore when done.
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
            // GridLayer fires "tileerror" per failed tile, never a bare "error" -
            // the latter is just Evented's generic type surface and Leaflet's own
            // tile-loading code never emits it, so this previously never ran and a
            // hard tile failure could leave .tiles-loading stuck indefinitely.
            layer.on("tileerror", onLoad);
        }
    }

    // -- Button syncing ---------------------------------------------------------------
    function syncButtons(): void {
        const state = getState();
        panel.sync({ base: state.base, weather: state.weather, borders: state.borders, dark: isDarkActive(), custom });
    }

    // -- Base / overlay switching --------------------------------------------------------
    function setBase(rawKey: string): void {
        const key = normalizeBase(rawKey);
        if (key !== "satellite" && map.hasLayer(satelliteLayer)) map.removeLayer(satelliteLayer);
        if (key !== "topographic" && map.hasLayer(topographicLayer)) map.removeLayer(topographicLayer);
        if (key === "satellite" && !map.hasLayer(satelliteLayer)) satelliteLayer.addTo(map);
        if (key === "topographic" && !map.hasLayer(topographicLayer)) topographicLayer.addTo(map);
        // Whether the base beneath is worth drawing depends on what is now above it.
        syncBaseLayer();
        syncButtons();
        persistState();
    }

    // Button semantics from the main map: street always selects street.
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
        syncBaseLayer();
        syncButtons();
    }

    function toggleDark(): void {
        const newMode: MapDarkMode = darkMode === "dark" ? "light" : "dark";
        setDarkMode(newMode);
        opts.onDarkModeChange?.(newMode);
        persistState();
    }

    // -- Flyout panel and button wiring -------------------------------------------------
    const panel = createLayersPanel(root, !!weather, { toggleBase, toggleWeather, toggleBorders, toggleDark, toggleCustom });

    // -- Initial state -------------------------------------------------------------------------
    (function applyInitialLayers() {
        let base = configuredBase;
        let weatherOn = (opts.initialOverlays || []).includes("weather");
        const bordersOn = (opts.initialOverlays || []).includes("borders");

        // Choosing "Remember" says nothing about a first visit, and nothing is remembered for a map
        // with nowhere to store it, so both land on the same base a viewer who set nothing gets.
        if (base === "remember") {
            base = DEFAULT_BASE_LAYER;
            if (remember) {
                try {
                    const saved = JSON.parse(localStorage.getItem(opts.storageKey!) || "null");
                    if (saved) {
                        base = saved.base || DEFAULT_BASE_LAYER;
                        weatherOn = !!saved.weather;
                    }
                } catch {
                    /* corrupt storage - nothing was remembered */
                }
            }
            base = resolveConfiguredBase(root, base);
        }

        const key = normalizeBase(base);
        if (key === "satellite") satelliteLayer.addTo(map);
        else if (key === "topographic") topographicLayer.addTo(map);

        if (weatherOn && weather) {
            weather.rain.addTo(map);
            weather.clouds.addTo(map);
        }
        if (bordersOn) bordersLayer.addTo(map);
        // After the base is on the map, never before: it is what decides whether a base underneath
        // would be covered, and a sync that runs first adds one that then stays for the session.
        syncBaseLayer();
        syncButtons();
    })();

    // Every map that uses this engine gets the same right-click menu unless
    // the page has already claimed contextmenu for something else.
    const unbindContextMenu =
        opts.contextMenu !== false ? bindMapContextMenu(map, typeof opts.contextMenu === "object" ? opts.contextMenu : {}) : null;

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
            map.off("layeradd layerremove", onTopoLayerChange);
            if (opts.onAttribution) map.off("layeradd layerremove", onAttributionLayerChange);
            if (attributionFrame !== null) {
                window.cancelAnimationFrame(attributionFrame);
                attributionFrame = null;
            }
            if (colorSchemeQuery && onColorSchemeChange) colorSchemeQuery.removeEventListener("change", onColorSchemeChange);
            panel.destroy();
            unbindContextMenu?.();
        },
    };
}

/** The one element on the site that shows which tiles are being drawn (`partials/layout/footer.html`). */
const FOOTER_ATTRIBUTION_ID = "page-footer-attribution-text";

/** Attribution reported before the footer was parsed, waiting for it. */
let pendingAttribution: string | null = null;

function writeFooterAttribution(text: string): boolean {
    const el = document.getElementById(FOOTER_ATTRIBUTION_ID);
    if (!el) return false;
    el.textContent = text;
    return true;
}

/**
 * Shows `text` as the footer's tile attribution - the `onAttribution` every map on the site wants.
 *
 * Several maps are built by an inline script in the page body, which runs before the footer include
 * further down it, so a map's first attribution can arrive before there is anywhere to put it.
 * Writing it when the document finishes parsing is what makes those pages credit their tiles at
 * all, rather than only from the first layer switch onwards.
 * @param text - Attribution for the layers currently drawn.
 */
export function setAttribution(text: string): void {
    if (writeFooterAttribution(text)) return;
    // Parsing is over and there is still no footer, so this page simply has none.
    if (document.readyState !== "loading") return;
    const waiting = pendingAttribution !== null;
    pendingAttribution = text;
    if (waiting) return;
    document.addEventListener(
        "DOMContentLoaded",
        () => {
            // The latest wins: a layer switched while the page was still parsing should not be
            // undone by whatever the map happened to report first.
            if (pendingAttribution !== null) writeFooterAttribution(pendingAttribution);
            pendingAttribution = null;
        },
        { once: true },
    );
}

export const MapLayers = {
    create: createMapLayers,
    setAttribution,
    tileLayer,
    rasterSourceFor,
    bordersOverlay,
    weatherLayers,
    normalizeBase,
    registerRedataLayers,
    vectorStyleFor,
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
