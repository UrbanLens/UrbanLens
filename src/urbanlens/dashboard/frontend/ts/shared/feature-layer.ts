/**
 * A plugin's layer on the Private Pin map: an outline-and-label FeatureCollection fetched from the pin's own endpoint.
 *
 * The endpoint carries the presentation (see `services/map/pin_layers.py`): each feature's `color`, `label` and
 * `name`, and an optional `notice` to show when the layer loads. Off until toggled, and fetched once per page. An area
 * usually covers the whole view, so it is filled faintly in a pane beneath the page's own boundaries and markup, and
 * labelled only where the pointer rests (see map-tooltips.ts).
 */

import { escHtml } from "./escape-html";
import { fetchJson, type FetchJsonOptions } from "./fetch-json";
import type { CustomLayerToggle } from "./map-layers";
import { bindAreaTooltip } from "./map-tooltips";

declare const L: typeof import("leaflet");

/** Above the tile overlays (401), below the page's boundary (540) and markup (550) panes. */
export const FEATURE_LAYER_PANE = "featureLayerPane";
const FEATURE_LAYER_PANE_Z_INDEX = "420";

const FALLBACK_COLOR = "#7a5c99";

/** One layer the page offers, as the server embeds it in `data-feature-layers`. */
export interface FeatureLayerConfig {
    key: string;
    url: string;
    attribution: string;
}

export interface FeatureLayerProps {
    color?: string;
    label?: string;
    name?: string;
}

export interface FeatureLayerNotice {
    kind: "info" | "warning";
    message: string;
}

export interface FeatureLayerResponse extends GeoJSON.FeatureCollection<GeoJSON.Geometry, FeatureLayerProps> {
    notice?: FeatureLayerNotice;
}

export interface FeatureLayer extends CustomLayerToggle {
    /** Settles once the current fetch has; resolves at once when none is running. */
    whenLoaded(): Promise<void>;
}

type Fetcher = <T>(url: string, options?: FetchJsonOptions) => Promise<T | null>;

/** The layers the page offers; none when the attribute is absent or is not a list of layers. */
export function parseFeatureLayers(raw: string | undefined): FeatureLayerConfig[] {
    if (!raw) return [];
    try {
        const parsed: unknown = JSON.parse(raw);
        if (!Array.isArray(parsed)) return [];
        return parsed.filter(
            (entry): entry is FeatureLayerConfig => !!entry && typeof entry.key === "string" && typeof entry.url === "string" && typeof entry.attribution === "string",
        );
    } catch {
        return [];
    }
}

/** A feature's path style: its own colour, dashed, and filled faintly. */
export function featureStyle(feature?: GeoJSON.Feature<GeoJSON.Geometry, FeatureLayerProps>): L.PathOptions {
    const color = feature?.properties?.color || FALLBACK_COLOR;
    return { color, weight: 2.5, opacity: 0.9, dashArray: "6 5", fillColor: color, fillOpacity: 0.08 };
}

/** The resting-pointer label for a feature, escaped. */
export function featureLabelHtml(props: FeatureLayerProps): string {
    const label = props.label ?? "";
    return `<strong>${escHtml(props.name || label)}</strong><br>${escHtml(label)}`;
}

/**
 * Creates one layer's toggle for the layers panel.
 * @param map - The pin's map.
 * @param config - The layer: its panel key and data endpoint.
 * @param notify - Shows a notice, as a toast on the page.
 * @param fetcher - The JSON fetch, replaced in tests.
 * @returns The toggle, off.
 */
export function createFeatureLayer(map: L.Map, config: FeatureLayerConfig, notify: (notice: FeatureLayerNotice) => void, fetcher: Fetcher = fetchJson): FeatureLayer {
    if (!map.getPane(FEATURE_LAYER_PANE)) map.createPane(FEATURE_LAYER_PANE).style.zIndex = FEATURE_LAYER_PANE_Z_INDEX;
    const group = L.layerGroup();
    let active = false;
    let loaded = false;
    let pending: Promise<void> | null = null;

    function setLoading(loading: boolean): void {
        const button = document.querySelector(`[data-map-layer="${config.key}"]`);
        button?.classList.toggle("is-loading", loading);
        button?.setAttribute("aria-busy", loading ? "true" : "false");
    }

    function load(): Promise<void> {
        setLoading(true);
        return fetcher<FeatureLayerResponse>(config.url, { headers: { "X-Requested-With": "XMLHttpRequest" }, reportsItsOwnErrors: true })
            .then((data) => {
                if (!data) throw new Error("Could not load this layer.");
                group.clearLayers();
                L.geoJSON<FeatureLayerProps>(data, {
                    pane: FEATURE_LAYER_PANE,
                    style: featureStyle,
                    onEachFeature: (feature, layer) => bindAreaTooltip(map, layer, featureLabelHtml(feature.properties), { className: "boundary-tooltip" }),
                }).addTo(group);
                loaded = true;
                if (data.notice && active) notify(data.notice);
            })
            .catch((error: unknown) => {
                if (active) notify({ kind: "warning", message: error instanceof Error && error.message ? error.message : "Could not load this layer." });
            })
            .finally(() => {
                pending = null;
                setLoading(false);
            });
    }

    return {
        isActive: () => active,
        toggle(): void {
            active = !active;
            if (!active) {
                map.removeLayer(group);
                return;
            }
            group.addTo(map);
            if (!loaded && !pending) pending = load();
        },
        whenLoaded: () => pending ?? Promise.resolve(),
    };
}
