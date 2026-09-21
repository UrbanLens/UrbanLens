/**
 * The map surface a page will use, independent of which engine draws it.
 *
 * Only `isMaplibreMap` below has a live caller. The camera, viewport and event contract is the
 * unwired facade `map-cluster-group.ts` describes - every page still drives Leaflet directly.
 *
 * `map-layers.ts` already dual-engines the *basemap*; this is the rest of what a page does with a
 * map - camera, viewport, pointer geometry and events - so a call site can be written once and run
 * on either engine (`D12`'s dual-engine requirement, `PL8`). Leaflet stays a genuine second engine
 * for the browsers without WebGL2, so neither implementation is a migration stopgap.
 *
 * The shapes here are deliberately plain: `{lat, lng}` objects and numbers rather than either
 * engine's own classes, because an `L.LatLng` leaking into shared code is how a "port" ends up
 * still needing Leaflet loaded.
 */

import type { Map as MaplibreMap } from "maplibre-gl";

declare const L: typeof import("leaflet");

export type MapEngineKind = "leaflet" | "maplibre";

/**
 * Whether `map` is a MapLibre map rather than a Leaflet one.
 *
 * Duck-typed on a MapLibre-only method rather than `instanceof maplibregl.Map`, because
 * `maplibregl` is a CDN global that is simply absent on pages that never load it.
 *
 * Lives in this module, which imports nothing, so the engine test costs a caller no bundle: the
 * marker and cluster facades need it and have no other reason to reach the layers engine.
 * @param map - The map to test.
 */
export function isMaplibreMap(map: object): map is MaplibreMap {
    return typeof (map as Partial<MaplibreMap>).setLayoutProperty === "function";
}

export interface LatLng {
    lat: number;
    lng: number;
}

/** A rectangle in geographic coordinates, in the terms both engines can answer in. */
export interface MapViewBounds {
    south: number;
    west: number;
    north: number;
    east: number;
}

export interface ScreenPoint {
    x: number;
    y: number;
}

/** A pointer event on the map, with the geography already resolved. */
export interface MapPointerEvent {
    lat: number;
    lng: number;
    originalEvent: MouseEvent;
}

/** Events a page listens for. `popupopen` carries the popup's own root element. */
export interface MapViewEvents {
    move: void;
    moveend: void;
    zoomend: void;
    click: MapPointerEvent;
    contextmenu: MapPointerEvent;
    popupopen: { element: HTMLElement | null };
}

export type MapViewEventName = keyof MapViewEvents;

export interface FitBoundsOptions {
    /** Uniform padding in pixels, or per-corner when the page has overlaying chrome. */
    padding?: number | { topLeft: [number, number]; bottomRight: [number, number] };
    maxZoom?: number;
}

export interface MapView {
    readonly kind: MapEngineKind;
    /** The engine's own map, for the shrinking set of call sites that still need one. */
    readonly native: unknown;

    setView(center: LatLng, zoom?: number): void;
    getCenter(): LatLng;
    getZoom(): number;
    getMinZoom(): number;
    getMaxZoom(): number;
    getBounds(): MapViewBounds;
    fitBounds(bounds: MapViewBounds, options?: FitBoundsOptions): void;

    getContainer(): HTMLElement;
    /** Where a coordinate currently sits within the container, for anchoring page chrome to it. */
    latLngToContainerPoint(position: LatLng): ScreenPoint;
    /** Where a pointer event landed, in geography. */
    pointerToLatLng(event: MouseEvent): LatLng;

    /** Whether the user may pan by dragging - turned off while a rubber-band selection is drawn. */
    setDraggingEnabled(enabled: boolean): void;
    /** Re-reads the container's size after the page has resized it. */
    resized(): void;
    closePopup(): void;

    on<E extends MapViewEventName>(event: E, handler: (payload: MapViewEvents[E]) => void): void;
    once<E extends MapViewEventName>(event: E, handler: (payload: MapViewEvents[E]) => void): void;
    off<E extends MapViewEventName>(event: E, handler: (payload: MapViewEvents[E]) => void): void;
}

/** Whether these bounds contain a point, on the same closed-interval terms both engines use. */
export function boundsContain(bounds: MapViewBounds, position: LatLng): boolean {
    return position.lat >= bounds.south && position.lat <= bounds.north && position.lng >= bounds.west && position.lng <= bounds.east;
}

/** The `west,south,east,north` string this app's own bbox query parameters are built from. */
export function boundsToBBoxString(bounds: MapViewBounds): string {
    return `${bounds.west},${bounds.south},${bounds.east},${bounds.north}`;
}

/** Bounds covering every position given, or null for an empty list. */
export function boundsOf(positions: LatLng[]): MapViewBounds | null {
    if (positions.length === 0) return null;
    const lats = positions.map((position) => position.lat);
    const lngs = positions.map((position) => position.lng);
    return { south: Math.min(...lats), north: Math.max(...lats), west: Math.min(...lngs), east: Math.max(...lngs) };
}

// -- Leaflet ---------------------------------------------------------------------------------

/** Leaflet's own event names for each shared one; `move` and the rest map one-to-one. */
const LEAFLET_EVENTS: Record<MapViewEventName, string> = {
    move: "move",
    moveend: "moveend",
    zoomend: "zoomend",
    click: "click",
    contextmenu: "contextmenu",
    popupopen: "popupopen",
};

export function createLeafletMapView(map: L.Map): MapView {
    /**
     * One wrapper per (handler, event) pair, so `off` hands Leaflet back the same function `on`
     * gave it - a fresh closure each time would leave every listener attached forever.
     */
    const wrappers = new WeakMap<object, Map<string, L.LeafletEventHandlerFn>>();

    function wrapped<E extends MapViewEventName>(event: E, handler: (payload: MapViewEvents[E]) => void): L.LeafletEventHandlerFn | undefined {
        return wrappers.get(handler)?.get(event);
    }

    function wrap<E extends MapViewEventName>(event: E, handler: (payload: MapViewEvents[E]) => void): L.LeafletEventHandlerFn {
        const existing = wrapped(event, handler);
        if (existing) return existing;
        const forHandler = wrappers.get(handler) ?? new Map<string, L.LeafletEventHandlerFn>();
        wrappers.set(handler, forHandler);
        // Narrowed by the fields Leaflet's own event subtypes carry, rather than by a cast to the
        // subtype this event name implies - which would be an assertion about Leaflet, not a check.
        const wrapper: L.LeafletEventHandlerFn = (raw) => {
            if ("latlng" in raw) {
                const mouse = raw as L.LeafletMouseEvent;
                handler({ lat: mouse.latlng.lat, lng: mouse.latlng.lng, originalEvent: mouse.originalEvent } as MapViewEvents[E]);
            } else if ("popup" in raw) {
                handler({ element: (raw as L.PopupEvent).popup.getElement() ?? null } as MapViewEvents[E]);
            } else {
                handler(undefined as MapViewEvents[E]);
            }
        };
        forHandler.set(event, wrapper);
        return wrapper;
    }

    return {
        kind: "leaflet",
        native: map,
        setView: (center, zoom) => void map.setView([center.lat, center.lng], zoom),
        getCenter: () => {
            const centre = map.getCenter();
            return { lat: centre.lat, lng: centre.lng };
        },
        getZoom: () => map.getZoom(),
        getMinZoom: () => map.getMinZoom(),
        getMaxZoom: () => map.getMaxZoom(),
        getBounds: () => {
            const bounds = map.getBounds();
            return { south: bounds.getSouth(), west: bounds.getWest(), north: bounds.getNorth(), east: bounds.getEast() };
        },
        fitBounds: (bounds, options = {}) => {
            const padding =
                typeof options.padding === "object"
                    ? { paddingTopLeft: options.padding.topLeft, paddingBottomRight: options.padding.bottomRight }
                    : { padding: [options.padding ?? 0, options.padding ?? 0] as [number, number] };
            map.fitBounds(
                [
                    [bounds.south, bounds.west],
                    [bounds.north, bounds.east],
                ],
                { ...padding, maxZoom: options.maxZoom },
            );
        },
        getContainer: () => map.getContainer(),
        latLngToContainerPoint: (position) => {
            const point = map.latLngToContainerPoint([position.lat, position.lng]);
            return { x: point.x, y: point.y };
        },
        pointerToLatLng: (event) => {
            const position = map.mouseEventToLatLng(event);
            return { lat: position.lat, lng: position.lng };
        },
        setDraggingEnabled: (enabled) => (enabled ? map.dragging.enable() : map.dragging.disable()),
        resized: () => void map.invalidateSize(),
        closePopup: () => void map.closePopup(),
        on: (event, handler) => void map.on(LEAFLET_EVENTS[event], wrap(event, handler)),
        once: (event, handler) => void map.once(LEAFLET_EVENTS[event], wrap(event, handler)),
        off: (event, handler) => {
            const wrapper = wrapped(event, handler);
            if (wrapper) map.off(LEAFLET_EVENTS[event], wrapper);
        },
    };
}
