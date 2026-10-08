/**
 * The Private Pin map's Land Use layer: the Census Special Land Use Areas (military installations, prisons, national
 * parks, college campuses) a pin's parcel falls inside, from `PinController.land_use_areas_json`.
 *
 * Off until toggled, and fetched once per page. An area usually covers the whole view, so it is filled faintly in a pane
 * beneath the page's own boundaries and markup, and labelled only where the pointer rests (see map-tooltips.ts).
 */

import { escHtml } from "./escape-html";
import { fetchJson, type FetchJsonOptions } from "./fetch-json";
import type { CustomLayerToggle } from "./map-layers";
import { bindAreaTooltip } from "./map-tooltips";

declare const L: typeof import("leaflet");

/** Above the tile overlays (401), below the page's boundary (540) and markup (550) panes. */
export const LAND_USE_PANE = "landUsePane";
const LAND_USE_PANE_Z_INDEX = "420";

export interface LandUseAreaProps {
    category: string;
    label: string;
    name: string;
}

export type LandUseStatus = "found" | "none" | "no_parcel" | "unavailable" | "outside_coverage";

export interface LandUseAreasResponse extends GeoJSON.FeatureCollection<GeoJSON.Polygon | GeoJSON.MultiPolygon, LandUseAreaProps> {
    status: LandUseStatus;
    /** False when REData drew fewer areas than the parcel record says the parcel is inside. */
    complete: boolean;
    attribution?: string;
}

export interface LandUseNotice {
    kind: "info" | "warning";
    message: string;
}

export interface LandUseLayer extends CustomLayerToggle {
    /** Settles once the current fetch has; resolves at once when none is running. */
    whenLoaded(): Promise<void>;
}

type Fetcher = <T>(url: string, options?: FetchJsonOptions) => Promise<T | null>;

const COLORS: Record<string, string> = {
    military_installation: "#556b2f",
    correctional_facility: "#b23b3b",
    national_park: "#2e7d32",
    college_university: "#1f5fa8",
};
const FALLBACK_COLOR = "#7a5c99";

const EMPTY_MESSAGES: Record<Exclude<LandUseStatus, "found">, string> = {
    none: "This pin isn't inside a military installation, prison, national park or college campus.",
    no_parcel: "There's no parcel record for this pin, so there are no land-use boundaries to show.",
    unavailable: "Land-use boundaries aren't available here right now.",
    outside_coverage: "Land-use boundaries cover the United States only.",
};

/** The outline colour of an area's category. */
export function landUseColor(category: string): string {
    return COLORS[category] ?? FALLBACK_COLOR;
}

/** An area's path style: its category's colour, dashed, and filled faintly. */
export function landUseStyle(feature?: GeoJSON.Feature<GeoJSON.Geometry, LandUseAreaProps>): L.PathOptions {
    const color = landUseColor(feature?.properties?.category ?? "");
    return { color, weight: 2.5, opacity: 0.9, dashArray: "6 5", fillColor: color, fillOpacity: 0.08 };
}

/** The resting-pointer label for an area, escaped. */
export function landUseLabelHtml(props: LandUseAreaProps): string {
    return `<strong>${escHtml(props.name || props.label)}</strong><br>${escHtml(props.label)}`;
}

/** What to tell the viewer about an answer: the areas found, or why there is nothing to draw. */
export function landUseNotice(data: LandUseAreasResponse): LandUseNotice | null {
    if (data.status !== "found" || !data.features.length) {
        return { kind: "info", message: EMPTY_MESSAGES[data.status === "found" ? "none" : data.status] ?? EMPTY_MESSAGES.unavailable };
    }
    if (!data.complete) return { kind: "warning", message: "Some land-use boundaries couldn't be loaded right now. Turn the layer off and on again later to retry." };
    const names = data.features.map((feature) => `${feature.properties.name} (${feature.properties.label})`);
    return { kind: "info", message: `This pin is inside ${names.join(", ")}.` };
}

/**
 * Creates the layer's toggle for the layers panel.
 * @param map - The pin's map.
 * @param url - The pin's land-use areas JSON endpoint.
 * @param notify - Shows a notice, as a toast on the page.
 * @param fetcher - The JSON fetch, replaced in tests.
 * @returns The toggle, off.
 */
export function createLandUseLayer(map: L.Map, url: string, notify: (notice: LandUseNotice) => void, fetcher: Fetcher = fetchJson): LandUseLayer {
    if (!map.getPane(LAND_USE_PANE)) map.createPane(LAND_USE_PANE).style.zIndex = LAND_USE_PANE_Z_INDEX;
    const group = L.layerGroup();
    let active = false;
    let loaded = false;
    let pending: Promise<void> | null = null;

    function setLoading(loading: boolean): void {
        const button = document.querySelector('[data-map-layer="landuse"]');
        button?.classList.toggle("is-loading", loading);
        button?.setAttribute("aria-busy", loading ? "true" : "false");
    }

    function load(): Promise<void> {
        setLoading(true);
        return fetcher<LandUseAreasResponse>(url, { headers: { "X-Requested-With": "XMLHttpRequest" }, reportsItsOwnErrors: true })
            .then((data) => {
                if (!data) throw new Error("Could not load land-use boundaries.");
                group.clearLayers();
                L.geoJSON<LandUseAreaProps>(data, {
                    pane: LAND_USE_PANE,
                    style: landUseStyle,
                    onEachFeature: (feature, layer) => bindAreaTooltip(map, layer, landUseLabelHtml(feature.properties), { className: "boundary-tooltip" }),
                }).addTo(group);
                loaded = true;
                const notice = landUseNotice(data);
                if (notice && active) notify(notice);
            })
            .catch((error: unknown) => {
                if (active) notify({ kind: "warning", message: error instanceof Error && error.message ? error.message : "Could not load land-use boundaries." });
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
