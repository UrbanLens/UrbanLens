/**
 * The MapLibre half of `map-view.ts` - the same `MapView` contract the Leaflet half satisfies.
 *
 * Three places the engines genuinely differ, rather than merely spelling the same thing:
 *
 * 1. **Coordinate order.** MapLibre is `[lng, lat]` throughout; Leaflet is `[lat, lng]`. The
 *    contract is `{lat, lng}` precisely so no call site has to remember which one it is talking to.
 * 2. **Popups are not the map's.** Leaflet's map owns the open popup, so it can close it and fire
 *    `popupopen` itself. MapLibre's popups are free-standing objects, so this view is told about
 *    them (`trackPopup`) and keeps the two behaviours the pages rely on.
 * 3. **Pointer geometry.** Leaflet's `mouseEventToLatLng` takes a page-coordinate event; MapLibre's
 *    `unproject` wants container-relative pixels, so the container's own rectangle is subtracted.
 */

import type { LngLatBoundsLike, Map as MaplibreMap, MapMouseEvent } from "maplibre-gl";

import type { FitBoundsOptions, LatLng, MapPointerEvent, MapView, MapViewBounds, MapViewEventName, MapViewEvents, ScreenPoint } from "./map-view";

/** A popup this view should be able to close and report on, whoever created it. */
export interface TrackedPopup {
    isOpen(): boolean;
    close(): void;
    element(): HTMLElement | null;
}

/** MapLibre's own event names for each shared one. `popupopen` has none and is fired by this view. */
const MAPLIBRE_EVENTS: Record<Exclude<MapViewEventName, "popupopen">, string> = {
    move: "move",
    moveend: "moveend",
    zoomend: "zoomend",
    click: "click",
    contextmenu: "contextmenu",
};

export interface MaplibreMapView extends MapView {
    /**
     * Puts a popup under this view's control, so `closePopup()` reaches it and opening it fires
     * `popupopen` - both of which Leaflet's map does for itself.
     * @returns A function that stops tracking it.
     */
    trackPopup(popup: TrackedPopup): () => void;
    /** Announces that `popup` has just opened, for the `popupopen` listeners. */
    popupOpened(popup: TrackedPopup): void;
}

export function createMaplibreMapView(map: MaplibreMap): MaplibreMapView {
    const wrappers = new WeakMap<object, Map<string, (raw: unknown) => void>>();
    const popupListeners = new Set<(payload: MapViewEvents["popupopen"]) => void>();
    const oncePopupListeners = new Set<(payload: MapViewEvents["popupopen"]) => void>();
    const tracked = new Set<TrackedPopup>();

    function wrapped<E extends MapViewEventName>(event: E, handler: (payload: MapViewEvents[E]) => void): ((raw: unknown) => void) | undefined {
        return wrappers.get(handler)?.get(event);
    }

    function wrap<E extends MapViewEventName>(event: E, handler: (payload: MapViewEvents[E]) => void): (raw: unknown) => void {
        const existing = wrapped(event, handler);
        if (existing) return existing;
        const forHandler = wrappers.get(handler) ?? new Map<string, (raw: unknown) => void>();
        wrappers.set(handler, forHandler);
        // Narrowed by the field MapLibre's mouse events carry, not by the event name.
        const wrapper = (raw: unknown): void => {
            if (raw && typeof raw === "object" && "lngLat" in raw) {
                const mouse = raw as MapMouseEvent;
                handler({ lat: mouse.lngLat.lat, lng: mouse.lngLat.lng, originalEvent: mouse.originalEvent } as MapViewEvents[E]);
            } else {
                handler(undefined as MapViewEvents[E]);
            }
        };
        forHandler.set(event, wrapper);
        return wrapper;
    }

    function fitPadding(options: FitBoundsOptions): number | { top: number; bottom: number; left: number; right: number } {
        if (typeof options.padding !== "object") return options.padding ?? 0;
        const [left, top] = options.padding.topLeft;
        const [right, bottom] = options.padding.bottomRight;
        return { top, bottom, left, right };
    }

    const view: MaplibreMapView = {
        kind: "maplibre",
        native: map,

        setView: (center, zoom) => void map.jumpTo(zoom === undefined ? { center: [center.lng, center.lat] } : { center: [center.lng, center.lat], zoom }),
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
            const box: LngLatBoundsLike = [
                [bounds.west, bounds.south],
                [bounds.east, bounds.north],
            ];
            // `animate: false` matches Leaflet's own default for fitBounds, which this replaces.
            map.fitBounds(box, { padding: fitPadding(options), maxZoom: options.maxZoom, animate: false });
        },

        getContainer: () => map.getContainer(),
        latLngToContainerPoint: (position): ScreenPoint => {
            const point = map.project([position.lng, position.lat]);
            return { x: point.x, y: point.y };
        },
        pointerToLatLng: (event): LatLng => {
            const box = map.getContainer().getBoundingClientRect();
            const position = map.unproject([event.clientX - box.left, event.clientY - box.top]);
            return { lat: position.lat, lng: position.lng };
        },

        setDraggingEnabled: (enabled) => (enabled ? map.dragPan.enable() : map.dragPan.disable()),
        resized: () => void map.resize(),
        closePopup: () => {
            for (const popup of tracked) if (popup.isOpen()) popup.close();
        },

        on: (event, handler) => {
            if (event === "popupopen") {
                popupListeners.add(handler as (payload: MapViewEvents["popupopen"]) => void);
                return;
            }
            map.on(MAPLIBRE_EVENTS[event as Exclude<MapViewEventName, "popupopen">], wrap(event, handler));
        },
        once: (event, handler) => {
            if (event === "popupopen") {
                oncePopupListeners.add(handler as (payload: MapViewEvents["popupopen"]) => void);
                return;
            }
            map.once(MAPLIBRE_EVENTS[event as Exclude<MapViewEventName, "popupopen">], wrap(event, handler));
        },
        off: (event, handler) => {
            if (event === "popupopen") {
                popupListeners.delete(handler as (payload: MapViewEvents["popupopen"]) => void);
                oncePopupListeners.delete(handler as (payload: MapViewEvents["popupopen"]) => void);
                return;
            }
            const wrapper = wrapped(event, handler);
            if (wrapper) map.off(MAPLIBRE_EVENTS[event as Exclude<MapViewEventName, "popupopen">], wrapper);
        },

        trackPopup: (popup) => {
            tracked.add(popup);
            return () => void tracked.delete(popup);
        },
        popupOpened: (popup) => {
            const payload = { element: popup.element() };
            for (const listener of popupListeners) listener(payload);
            const onces = [...oncePopupListeners];
            oncePopupListeners.clear();
            for (const listener of onces) listener(payload);
        },
    };
    return view;
}

/** Narrows a view to the MapLibre one, for the few call sites that need `trackPopup`. */
export function asMaplibreView(view: MapView): MaplibreMapView | null {
    return view.kind === "maplibre" ? (view as MaplibreMapView) : null;
}

export type { MapPointerEvent, MapViewBounds };
