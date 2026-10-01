/**
 * The MapLibre half of `map-markers.ts`.
 *
 * `maplibregl.Marker` wants its element at construction, where `L.Marker` builds one in `onAdd`.
 * Nothing here is allowed to cost a DOM element per pin held off-screen, so the MapLibre marker and
 * its element are built on the first `addTo` and torn down on `remove`; everything set before then
 * is kept as plain data and replayed. That is what keeps `getElement()` honest as well - it answers
 * null before the marker has ever been shown, exactly as Leaflet's does.
 *
 * Two behaviours are rebuilt rather than mapped, because MapLibre has no equivalent:
 *
 * - **Icon anchoring.** Leaflet's `iconAnchor` is pixels from the element's top-left to the
 *   coordinate. MapLibre positions by a named corner, so `top-left` plus a negated offset
 *   reproduces it exactly, for any icon size, without arithmetic at the call site.
 * - **Popups.** MapLibre's `marker.setPopup` installs its own click-to-toggle handler, which would
 *   race the page's own click handling and leave a popup that opens and closes on one click. The
 *   popup is therefore held here and positioned with the marker.
 */

import type { LatLng, MapView } from "./map-view";
import type { MapMarker, MapMarkerEventName, MapMarkerEvents, MapMarkerOptions, MarkerIcon } from "./map-markers";
import { asMaplibreView } from "./maplibre-view";
import type { MaplibreMapView } from "./maplibre-view";

import type { Map as MaplibreMap, Marker as MaplibreMarker, Popup as MaplibrePopup } from "maplibre-gl";

declare const maplibregl: typeof import("maplibre-gl");

/** DOM events that stand in for the marker events Leaflet fires; the rest come from MapLibre. */
const DOM_EVENTS: Partial<Record<MapMarkerEventName, string>> = {
    click: "click",
    contextmenu: "contextmenu",
    mouseover: "mouseenter",
    mouseout: "mouseleave",
};

function buildElement(icon: MarkerIcon, zIndexOffset: number | undefined, title: string | undefined): HTMLElement {
    const element = document.createElement("div");
    if (icon.className) element.className = icon.className;
    element.innerHTML = icon.html;
    element.style.width = `${icon.size[0]}px`;
    element.style.height = `${icon.size[1]}px`;
    if (zIndexOffset !== undefined) element.style.zIndex = String(zIndexOffset);
    if (title !== undefined) element.title = title;
    return element;
}

export function createMaplibreMarker(position: LatLng, options: MapMarkerOptions = {}): MapMarker {
    let at: LatLng = { ...position };
    let icon: MarkerIcon = options.icon ?? { html: "", size: [1, 1], anchor: [0, 0] };
    let draggable = options.draggable ?? false;
    let popupHtml: string | null = null;

    let marker: MaplibreMarker | null = null;
    let element: HTMLElement | null = null;
    let popup: MaplibrePopup | null = null;
    let map: MaplibreMap | null = null;
    let view: MaplibreMapView | null = null;
    let untrackPopup: (() => void) | null = null;

    const listeners = new Map<MapMarkerEventName, ((payload: never) => void)[]>();
    /** DOM listeners currently attached, so the element can be detached cleanly on `remove`. */
    let attached: { type: string; handler: EventListener }[] = [];

    function fire<E extends MapMarkerEventName>(event: E, payload: MapMarkerEvents[E]): void {
        for (const listener of listeners.get(event) ?? []) (listener as (value: MapMarkerEvents[E]) => void)(payload);
    }

    function pointerPayload(raw: MouseEvent): MapMarkerEvents["click"] {
        return { lat: at.lat, lng: at.lng, originalEvent: raw };
    }

    function popupOffset(): [number, number] {
        // The coordinate sits `anchor[1]` below the icon's top edge, so the popup clears the icon.
        return [0, -icon.anchor[1]];
    }

    function ensurePopup(): MaplibrePopup | null {
        if (popupHtml === null || map === null) return null;
        if (popup === null) {
            popup = new maplibregl.Popup({ offset: popupOffset(), className: "map-popup" });
            if (view) {
                untrackPopup = view.trackPopup({
                    isOpen: () => popup?.isOpen() ?? false,
                    close: () => void popup?.remove(),
                    element: () => popup?.getElement() ?? null,
                });
            }
        }
        popup.setHTML(popupHtml);
        popup.setLngLat([at.lng, at.lat]);
        return popup;
    }

    function openPopup(): void {
        const open = ensurePopup();
        if (!open || !map) return;
        open.addTo(map);
        view?.popupOpened({
            isOpen: () => open.isOpen(),
            close: () => void open.remove(),
            element: () => open.getElement() ?? null,
        });
    }

    function togglePopup(): void {
        if (popup?.isOpen()) popup.remove();
        else openPopup();
    }

    function attachDomListeners(target: HTMLElement): void {
        for (const [name, domEvent] of Object.entries(DOM_EVENTS) as [MapMarkerEventName, string][]) {
            const handler: EventListener = (raw) => {
                if (name === "click" || name === "contextmenu") {
                    // Leaflet's own bound-popup handler runs before the page's click listeners, and
                    // select-mode relies on that ordering: it closes a popup it can see is already
                    // open (`map-page.ts`'s `_handleSelectablePinClick`). Toggling after would
                    // instead close the popup that listener had just asked for.
                    if (name === "click" && popupHtml !== null) togglePopup();
                    fire(name, pointerPayload(raw as MouseEvent));
                    return;
                }
                fire(name, undefined as never);
            };
            target.addEventListener(domEvent, handler);
            attached.push({ type: domEvent, handler });
        }
    }

    function materialise(target: MaplibreMap): void {
        element = buildElement(icon, options.zIndexOffset, options.title);
        marker = new maplibregl.Marker({ element, anchor: "top-left", offset: [-icon.anchor[0], -icon.anchor[1]], draggable });
        marker.setLngLat([at.lng, at.lat]).addTo(target);
        attachDomListeners(element);
        marker.on("dragstart", () => fire("dragstart", undefined as never));
        marker.on("dragend", () => {
            const moved = marker?.getLngLat();
            if (moved) at = { lat: moved.lat, lng: moved.lng };
            popup?.setLngLat([at.lng, at.lat]);
            fire("dragend", undefined as never);
        });
    }

    function remove(): void {
        for (const { type, handler } of attached) element?.removeEventListener(type, handler);
        attached = [];
        popup?.remove();
        untrackPopup?.();
        untrackPopup = null;
        popup = null;
        marker?.remove();
        marker = null;
        element = null;
        map = null;
        view = null;
    }

    return {
        // A getter, not a field: there is no MapLibre marker to hand out until the pin is shown.
        get native(): unknown {
            return marker;
        },
        getLatLng: () => ({ ...at }),
        setLatLng: (next) => {
            at = { ...next };
            marker?.setLngLat([at.lng, at.lat]);
            popup?.setLngLat([at.lng, at.lat]);
        },
        setIcon: (next) => {
            icon = next;
            if (!element || !marker) return;
            // Rewriting the live element keeps the marker's identity, and with it any popup it owns.
            element.className = next.className ?? "";
            element.innerHTML = next.html;
            element.style.width = `${next.size[0]}px`;
            element.style.height = `${next.size[1]}px`;
            marker.setOffset([-next.anchor[0], -next.anchor[1]]);
            popup?.setOffset(popupOffset());
        },
        bindPopup: (html) => {
            popupHtml = html;
            if (popup) popup.setHTML(html);
        },
        openPopup,
        closePopup: () => void popup?.remove(),
        getElement: () => element,
        setDraggable: (enabled) => {
            draggable = enabled;
            marker?.setDraggable(enabled);
        },
        on: (event, handler) => {
            const forEvent = listeners.get(event) ?? [];
            forEvent.push(handler as (payload: never) => void);
            listeners.set(event, forEvent);
        },
        addTo: (target: MapView) => {
            const native = target.native as MaplibreMap;
            if (map === native) return;
            // Moving between maps has to give the old element and popup up first; leaving them
            // behind orphans a live marker on a map nothing holds a reference to any more.
            if (map !== null) remove();
            view = asMaplibreView(target);
            map = native;
            materialise(native);
        },
        remove,
        isOnMap: () => map !== null,
    };
}
