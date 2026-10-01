/**
 * The map on a pin shared with you (``pages/pin_share/detail.html``): MapLibre where the browser has WebGL2, Leaflet
 * otherwise (D12). A share with no location (hidden or rejected) still gets a map, so the toolbar's screenshot tool
 * has one to read.
 */

declare const L: typeof import("leaflet");
declare const maplibregl: typeof import("maplibre-gl") | undefined;

export interface SharedPinView {
    center: [number, number];
    zoom: number;
    point: [number, number] | null;
    name: string;
}

export interface SharedPinEngine {
    /** Builds the map, publishes it as ``window.map`` and gives it the site's layers panel. */
    createMap(el: HTMLElement, view: SharedPinView): void;
    addMarker(point: [number, number], name: string): void;
}

const CONTINENTAL_US: [number, number] = [39.8283, -98.5795];

export function sharedPinView(data: DOMStringMap): SharedPinView {
    const lat = Number.parseFloat(data.lat ?? "");
    const lng = Number.parseFloat(data.lng ?? "");
    const point: [number, number] | null = Number.isFinite(lat) && Number.isFinite(lng) ? [lat, lng] : null;
    return { center: point ?? CONTINENTAL_US, zoom: point ? 16 : 4, point, name: data.name ?? "" };
}

function layersFor(): Parameters<typeof window.MapLayers.create>[1] {
    return { root: document.getElementById("shared-pin-map-layers"), onAttribution: window.MapLayers.setAttribution };
}

function maplibreEngine(gl: typeof import("maplibre-gl")): SharedPinEngine {
    let map: import("maplibre-gl").Map | null = null;
    return {
        createMap(el, view) {
            // An empty style: MapLayers owns every tile layer, as on the Leaflet path.
            map = new gl.Map({ container: el, style: { version: 8, sources: {}, layers: [] }, center: [view.center[1], view.center[0]], zoom: view.zoom, maxZoom: 21, attributionControl: false });
            window.map = map;
            window.MapLayers.create(map, layersFor());
        },
        addMarker(point, name) {
            if (!map) return;
            new gl.Marker()
                .setLngLat([point[1], point[0]])
                .setPopup(new gl.Popup({ offset: 25 }).setText(name))
                .addTo(map)
                .togglePopup();
        },
    };
}

function leafletEngine(): SharedPinEngine {
    let map: L.Map | null = null;
    return {
        createMap(el, view) {
            map = L.map(el, { attributionControl: false }).setView(view.center, view.zoom);
            window.map = map;
            window.MapLayers.create(map, layersFor());
        },
        addMarker(point, name) {
            if (!map) return;
            // A node, not a string: Leaflet renders a string popup as HTML, and the sender chose this name.
            const label = document.createElement("span");
            label.textContent = name;
            L.marker(point).addTo(map).bindPopup(label).openPopup();
        },
    };
}

function pickEngine(): SharedPinEngine {
    return typeof maplibregl !== "undefined" && window.WebGLSupport?.supportsWebGL2() ? maplibreEngine(maplibregl) : leafletEngine();
}

export function installSharedPinMap(el: HTMLElement, engine: SharedPinEngine = pickEngine()): void {
    const view = sharedPinView(el.dataset);
    engine.createMap(el, view);
    if (view.point) engine.addMarker(view.point, view.name);
}
