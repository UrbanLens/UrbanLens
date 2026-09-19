/**
 * Builds a minimal MapLibre style document for a raster tile source - the D17
 * (docs/designs/basemap-self-hosting-fallback.md) mechanism: MapLibre's
 * "raster" source type is a drop-in for what Leaflet's TileLayer already
 * does, so drawing one of this app's existing vendor/proxy URLs needs no
 * vector data, no hosted style API, and (for self-hosters with no REData
 * configured) no third-party dependency beyond what this app already ships.
 * Not wired into any map yet - see docs/designs/leaflet-to-maplibre-migration.md (PL8).
 *
 * Deliberately does not import types from `maplibre-gl` or
 * `@maplibre/maplibre-gl-style-spec` - neither is an installed dependency yet
 * (only a CDN vendor-asset pin exists so far), so the shapes below are a
 * hand-verified subset of the real style spec (checked against
 * @maplibre/maplibre-gl-style-spec@24.8.1, the version maplibre-gl@5.24.0
 * itself depends on), not an import.
 */

/** A minimal MapLibre "raster" source - the fields this module actually sets. */
export interface MapLibreRasterSource {
    type: "raster";
    tiles: string[];
    tileSize: number;
    minzoom?: number;
    maxzoom?: number;
    attribution?: string;
}

/** A minimal MapLibre raster layer referencing a source of the same id. */
export interface MapLibreRasterLayer {
    id: string;
    type: "raster";
    source: string;
}

/** A minimal MapLibre style document containing exactly one raster source/layer pair. */
export interface MapLibreRasterStyle {
    version: 8;
    sources: Record<string, MapLibreRasterSource>;
    layers: MapLibreRasterLayer[];
}

/** Leaflet's own default `TileLayer` `subdomains` option (leaflet-src.js: `subdomains: 'abc'`) - the fallback when a source doesn't say otherwise. */
const LEAFLET_DEFAULT_SUBDOMAINS = ["a", "b", "c"];

/** What this module needs from one of this app's own tile sources (e.g. a `TILE_DEFS` entry). */
export interface RasterSourceInput {
    /** A Leaflet-style XYZ URL template - may use Leaflet's own `{s}`/`{r}` tokens; see `toMapLibreTileUrls`. */
    url: string;
    attribution?: string;
    minZoom?: number;
    maxNativeZoom?: number;
    /**
     * Mirrors Leaflet's own `TileLayerOptions.subdomains` (string or array;
     * `"abc"` and `["a","b","c"]` are equivalent). `TileDef.options` in
     * `map-layers.ts` is typed as the real `L.TileLayerOptions`, so a future
     * `TILE_DEFS` entry can set a custom `subdomains` with no compile error;
     * omit this to fall back to Leaflet's own default of `"abc"`.
     */
    subdomains?: string | string[];
}

/**
 * Expands a Leaflet-style XYZ template into the one or more literal URLs
 * MapLibre's TileJSON-shaped `tiles` array expects.
 *
 * MapLibre has no `{s}` (subdomain) or `{r}` (retina) token of its own -
 * verified against the actual `maplibre-gl@5.24.0` bundle's tile-URL
 * substitution logic, which only recognizes
 * `{prefix}`/`{z}`/`{x}`/`{y}`/`{ratio}`/`{quadkey}`/`{bbox-epsg-3857}`.
 * Passing a `{s}`/`{r}`-bearing template straight through would request the
 * literal substring "{s}" from the tile server on every request. `{s}` is
 * expanded into one URL per subdomain (TileJSON's own mechanism for
 * subdomain rotation is simply listing multiple URLs); `{r}` is dropped
 * outright rather than translated to MapLibre's `{ratio}` token, because no
 * entry in this app's own `TILE_DEFS` sets Leaflet's `detectRetina` option,
 * so `{r}` already always resolves to `""` under Leaflet today - dropping it
 * here preserves identical behavior rather than losing a capability this app
 * does not currently use.
 * @param leafletUrl - A URL template as stored in this app's own `TILE_DEFS`.
 * @param subdomains - Mirrors Leaflet's `TileLayerOptions.subdomains`; defaults to `"abc"` (Leaflet's own default) when omitted.
 * @returns One URL per subdomain if `leafletUrl` contains `{s}`, else a single-entry array.
 */
export function toMapLibreTileUrls(leafletUrl: string, subdomains?: string | string[]): string[] {
    const withoutRetina = leafletUrl.replace(/\{r\}/g, "");
    if (!withoutRetina.includes("{s}")) return [withoutRetina];
    const resolvedSubdomains = subdomains !== undefined ? Array.from(subdomains) : LEAFLET_DEFAULT_SUBDOMAINS;
    return resolvedSubdomains.map((subdomain) => withoutRetina.replace(/\{s\}/g, subdomain));
}

/**
 * Builds a one-source, one-layer MapLibre style document for `source`.
 * @param id - Used as both the source id and the layer id - this document only ever has one of each.
 * @param source - The tile source to wrap, in this app's own Leaflet-template shape (e.g. a `TILE_DEFS` entry).
 */
export function buildRasterStyle(id: string, source: RasterSourceInput): MapLibreRasterStyle {
    return {
        version: 8,
        sources: {
            [id]: {
                type: "raster",
                tiles: toMapLibreTileUrls(source.url, source.subdomains),
                // 256px to match every vendor this app uses - see BASE_ERROR_TILE_URL's own
                // comment in map-layers.ts, which draws its placeholder at the same size.
                tileSize: 256,
                minzoom: source.minZoom,
                maxzoom: source.maxNativeZoom,
                attribution: source.attribution,
            },
        },
        layers: [{ id, type: "raster", source: id }],
    };
}

export const MaplibreRasterStyle = { buildRasterStyle, toMapLibreTileUrls };

/** Publishes the builder on window for the classic inline template scripts and hand-written vanilla JS (e.g. `comment-map.js`). */
export function installGlobalMaplibreRasterStyle(): void {
    window.MaplibreRasterStyle = MaplibreRasterStyle;
}

declare global {
    interface Window {
        MaplibreRasterStyle: typeof MaplibreRasterStyle;
    }
}
