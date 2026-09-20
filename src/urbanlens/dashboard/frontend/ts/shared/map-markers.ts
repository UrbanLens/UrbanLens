/**
 * A pin on the map, independent of which engine draws it - the marker half of `map-view.ts`.
 *
 * Both engines anchor an HTML element at a coordinate, so this is a thin contract rather than an
 * abstraction over two different ideas. The one structural difference worth knowing: a marker's
 * element is created when it is first shown, not when it is built. Leaflet already works that way
 * (`L.Marker` builds its icon in `onAdd`), and the main map holds tens of thousands of markers of
 * which a viewport shows a few hundred, so building every element up front would cost a page load
 * for elements nobody sees. `getElement()` therefore answers `null` for a marker that is not
 * currently on a map - the same answer Leaflet gives, and what the selection-class call sites
 * already guard for.
 */

import type { LatLng, MapPointerEvent, MapView } from "./map-view";

declare const L: typeof import("leaflet");

/** What a marker draws: a block of HTML, its size, and which point inside it sits on the coordinate. */
export interface MarkerIcon {
    html: string;
    /** Class on the element itself. Leaflet's own `leaflet-div-icon` chrome is never wanted here. */
    className?: string;
    size: [number, number];
    /** Pixels from the element's top-left corner to the geographic point. */
    anchor: [number, number];
}

export interface MapMarkerEvents {
    click: MapPointerEvent;
    contextmenu: MapPointerEvent;
    mouseover: void;
    mouseout: void;
    dragstart: void;
    dragend: void;
}

export type MapMarkerEventName = keyof MapMarkerEvents;

export interface MapMarkerOptions {
    draggable?: boolean;
    icon?: MarkerIcon;
    /** Higher draws in front. Used for transient markers that must sit over the pins. */
    zIndexOffset?: number;
    title?: string;
}

export interface MapMarker {
    /** The engine's own marker, for the layer containers that can only take one of those. */
    readonly native: unknown;
    getLatLng(): LatLng;
    setLatLng(position: LatLng): void;
    setIcon(icon: MarkerIcon): void;
    /** Attaches a popup; replaces any previous one. */
    bindPopup(html: string): void;
    openPopup(): void;
    closePopup(): void;
    /** The live element, or null when this marker is not currently on a map. */
    getElement(): HTMLElement | null;
    setDraggable(enabled: boolean): void;
    on<E extends MapMarkerEventName>(event: E, handler: (payload: MapMarkerEvents[E]) => void): void;
    addTo(view: MapView): void;
    /** Takes it off whatever map it is on; safe to call when it is on none. */
    remove(): void;
    isOnMap(): boolean;
}

/**
 * Stops a pointer event reaching the map underneath.
 *
 * Both engines let a marker's own DOM event bubble to the container, where the map's `click` and
 * `contextmenu` handlers would also fire - so right-clicking a pin would open the pin's popup *and*
 * the blank-map "Add pin here" menu.
 */
export function stopPointerEvent(event: MapPointerEvent): void {
    event.originalEvent.preventDefault();
    event.originalEvent.stopPropagation();
}

// -- Leaflet ---------------------------------------------------------------------------------

function leafletIcon(icon: MarkerIcon): L.DivIcon {
    return L.divIcon({ className: icon.className ?? "", html: icon.html, iconSize: icon.size, iconAnchor: icon.anchor });
}

export function createLeafletMarker(position: LatLng, options: MapMarkerOptions = {}): MapMarker {
    const marker = L.marker([position.lat, position.lng], {
        draggable: options.draggable ?? false,
        zIndexOffset: options.zIndexOffset,
        title: options.title,
        ...(options.icon ? { icon: leafletIcon(options.icon) } : {}),
    });
    let map: L.Map | null = null;

    return {
        native: marker,
        getLatLng: () => {
            const at = marker.getLatLng();
            return { lat: at.lat, lng: at.lng };
        },
        setLatLng: (next) => void marker.setLatLng([next.lat, next.lng]),
        setIcon: (icon) => void marker.setIcon(leafletIcon(icon)),
        bindPopup: (html) => void marker.bindPopup(html),
        openPopup: () => void marker.openPopup(),
        closePopup: () => void marker.closePopup(),
        getElement: () => marker.getElement() ?? null,
        setDraggable: (enabled) => {
            if (!marker.dragging) return;
            if (enabled) marker.dragging.enable();
            else marker.dragging.disable();
        },
        on: (event, handler) => {
            marker.on(event, (raw: L.LeafletEvent) => {
                if ("latlng" in raw) {
                    const mouse = raw as L.LeafletMouseEvent;
                    handler({ lat: mouse.latlng.lat, lng: mouse.latlng.lng, originalEvent: mouse.originalEvent } as MapMarkerEvents[typeof event]);
                    return;
                }
                handler(undefined as MapMarkerEvents[typeof event]);
            });
        },
        addTo: (view) => {
            map = view.native as L.Map;
            marker.addTo(map);
        },
        remove: () => {
            marker.remove();
            map = null;
        },
        isOnMap: () => map !== null && map.hasLayer(marker),
    };
}
