/**
 * Drawing a basemap this deployment serves as a MapLibre style document rather than as XYZ raster
 * tiles - the `D11` vector shape, and the half of `PL8` that `maplibre-layers.ts` did not have.
 *
 * The style is merged into the live map's style rather than handed to `setStyle()`. `setStyle()`
 * replaces the whole document, which would drop every source and layer the page had already added
 * (pins, markup, boundaries) and force each call site to rebuild them on `styledata` - so switching
 * base layers would be a page-wide rebuild rather than a visibility flip. Merging keeps the engine's
 * existing contract: a style's sources and layers are added under a namespace, toggled like any
 * other managed layer, and removed again when another base is chosen.
 *
 * Two things MapLibre does for a style it loaded itself have to be done here instead, because a
 * document fetched by hand arrives with neither:
 *
 * 1. **Relative URLs.** MapLibre resolves a style's `tiles`/`url`/`glyphs`/`sprite` against the
 *    style's own address; a merged document would otherwise resolve them against the page.
 * 2. **Sprite and glyphs.** Both are document-level, not per-layer, so they are applied to the map
 *    with `addSprite`/`setGlyphs` and unwound on removal.
 */

import type { LayerSpecification, SourceSpecification, StyleSpecification } from "maplibre-gl";

/** One entry of a style's `sprite`, in the array form; a plain string style is normalised into this. */
export interface VectorStyleSprite {
    id: string;
    url: string;
}

/** A fetched style document rewritten so it can be merged into a map that already has layers. */
export interface NamespacedVectorStyle {
    sources: Record<string, SourceSpecification>;
    /** In the document's own order - MapLibre draws them bottom-to-top, and a basemap depends on it. */
    layers: LayerSpecification[];
    sprites: VectorStyleSprite[];
    glyphs: string | null;
}

/** The sprite id MapLibre resolves an unprefixed `icon-image` against. */
const DEFAULT_SPRITE_ID = "default";

/**
 * Whether `value` already names its own origin, and so needs no resolving.
 *
 * Covers both a scheme (`https:`, and the `ultile:` protocol `maplibre-raster-style.ts` registers)
 * and a protocol-relative `//host/path`.
 */
function isAbsoluteUrl(value: string): boolean {
    return /^[a-z][a-z0-9+.-]*:/i.test(value) || value.startsWith("//");
}

/**
 * Resolves one URL from a style document against the address that document was fetched from.
 *
 * `URL` percent-encodes braces, which would turn a `{z}/{x}/{y}` template into one that requests
 * the literal string `%7Bz%7D` from the tile server, so the two tokens MapLibre substitutes are put
 * back afterwards. A literal `%7B` in a source URL would be rewritten by this and is not expected
 * to occur; a tile template containing braces is the case that actually happens.
 * @param value - A URL or URL template as it appears in the style document.
 * @param styleUrl - Where the document was fetched from.
 * @returns An absolute URL, or `value` unchanged if it is already absolute or cannot be parsed.
 */
export function resolveStyleUrl(value: string, styleUrl: string): string {
    if (isAbsoluteUrl(value)) return value;
    try {
        return new URL(value, styleUrl).toString().replace(/%7B/gi, "{").replace(/%7D/gi, "}");
    } catch {
        return value;
    }
}

/** Resolves whichever URL-bearing fields this source actually has, leaving the rest untouched. */
function resolveSourceUrls(source: SourceSpecification, styleUrl: string): SourceSpecification {
    const resolved = { ...source } as SourceSpecification & { url?: string; tiles?: string[]; data?: unknown };
    if (typeof resolved.url === "string") resolved.url = resolveStyleUrl(resolved.url, styleUrl);
    if (Array.isArray(resolved.tiles)) resolved.tiles = resolved.tiles.map((tile) => resolveStyleUrl(tile, styleUrl));
    // A GeoJSON source's `data` is either an inline object or a URL; only the latter resolves.
    if (typeof resolved.data === "string") resolved.data = resolveStyleUrl(resolved.data, styleUrl);
    return resolved;
}

/** Normalises `sprite`'s two spec forms (one URL, or a list of id/url pairs) into the list form. */
function normaliseSprites(sprite: StyleSpecification["sprite"], styleUrl: string): VectorStyleSprite[] {
    if (typeof sprite === "string") return [{ id: DEFAULT_SPRITE_ID, url: resolveStyleUrl(sprite, styleUrl) }];
    if (!Array.isArray(sprite)) return [];
    return sprite
        .filter((entry): entry is VectorStyleSprite => !!entry && typeof entry.id === "string" && typeof entry.url === "string")
        .map((entry) => ({ id: entry.id, url: resolveStyleUrl(entry.url, styleUrl) }));
}

/**
 * Whether `value` is plausibly a MapLibre style document this module can merge.
 *
 * Deliberately shallow: every field below is read by this module, and a document that passes is
 * still validated properly by MapLibre itself when its sources and layers are added. The point is
 * to fail over to the raster fallback rather than throw, so a REData instance serving something
 * unexpected costs a deployment its vector layer and not its map.
 */
export function isVectorStyleDocument(value: unknown): value is StyleSpecification {
    if (!value || typeof value !== "object") return false;
    const doc = value as Partial<StyleSpecification>;
    return doc.version === 8 && !!doc.sources && typeof doc.sources === "object" && Array.isArray(doc.layers);
}

/**
 * Rewrites a fetched style document so its sources and layers can live alongside a page's own.
 *
 * Every source and layer id gains `prefix`, and each layer's `source` reference is remapped to
 * match. A layer naming a source the document does not define is dropped rather than added under a
 * dangling reference, which MapLibre rejects with an exception that would abort the whole merge.
 * `source-layer` is untouched: it names a layer inside the vector tile, not a source of this style.
 * @param prefix - Namespace for every id this style contributes.
 * @param doc - The fetched style document.
 * @param styleUrl - Where it was fetched from, for resolving its relative URLs.
 */
export function namespaceVectorStyle(prefix: string, doc: StyleSpecification, styleUrl: string): NamespacedVectorStyle {
    const renamedSources = new Map<string, string>();
    for (const id of Object.keys(doc.sources ?? {})) renamedSources.set(id, `${prefix}${id}`);

    const sources: Record<string, SourceSpecification> = {};
    for (const [id, source] of Object.entries(doc.sources ?? {})) {
        if (!source || typeof source !== "object") continue;
        sources[renamedSources.get(id)!] = resolveSourceUrls(source, styleUrl);
    }

    const layers: LayerSpecification[] = [];
    for (const layer of doc.layers ?? []) {
        if (!layer || typeof layer.id !== "string") continue;
        const renamed = { ...layer, id: `${prefix}${layer.id}` } as LayerSpecification & { source?: string };
        if ("source" in layer) {
            // `background` is the one layer type with no source at all; everything else must resolve.
            const mapped = renamedSources.get((layer as { source?: string }).source ?? "");
            if (mapped === undefined) continue;
            renamed.source = mapped;
        }
        layers.push(renamed);
    }

    return { sources, layers, sprites: normaliseSprites(doc.sprite, styleUrl), glyphs: doc.glyphs ? resolveStyleUrl(doc.glyphs, styleUrl) : null };
}

/**
 * Fetches and namespaces the style document at `styleUrl`.
 * @param prefix - Namespace for every id the style contributes.
 * @param styleUrl - The `style_url` a catalogue entry carried.
 * @param signal - Abandons the fetch when the base layer changes again before it lands.
 * @returns The merged-ready style, or `null` if it could not be had or is not a style document -
 * both of which leave the caller on its raster fallback.
 */
export async function fetchVectorStyle(prefix: string, styleUrl: string, signal?: AbortSignal): Promise<NamespacedVectorStyle | null> {
    try {
        const response = await fetch(styleUrl, { signal, headers: { Accept: "application/json" } });
        if (!response.ok) return null;
        const doc: unknown = await response.json();
        if (!isVectorStyleDocument(doc)) return null;
        return namespaceVectorStyle(prefix, doc, styleUrl);
    } catch {
        return null;
    }
}

/**
 * Not published on `window` like `MaplibreRasterStyle`/`WebGLSupport`: nothing outside the bundled
 * MapLibre engine consumes a style document, and a global with no caller is how `vectorStyleFor`
 * itself came to sit unused for a release.
 */
export const MaplibreVectorStyle = { fetchVectorStyle, namespaceVectorStyle, resolveStyleUrl, isVectorStyleDocument };
