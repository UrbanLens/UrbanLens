/**
 * Hover labels for map areas. A label that followed the pointer would never leave a polygon that fills the view, so
 * an area's label shows only once the pointer rests on it, and hides as soon as it moves or a map menu opens.
 */

import { isMapContextMenuOpen } from "./map-context-menu";

declare const L: typeof import("leaflet");

/** How long the pointer has to rest on an area before its label shows. */
export const AREA_TOOLTIP_REST_MS = 600;

const unbinders = new WeakMap<L.Layer, () => void>();

/**
 * Binds a label that shows where the pointer rests on ``layer``.
 *
 * The label is a tooltip of the map's, not bound to the layer, so none of Leaflet's own handlers can open it: only a
 * rest does.
 * @param map - The map the layer is on; its pans and zooms hide the label.
 * @param layer - The area: a polygon, circle or line.
 * @param html - The label's escaped HTML.
 * @param options - Leaflet tooltip options.
 * @param restMs - How long the pointer has to rest.
 */
export function bindAreaTooltip(map: L.Map, layer: L.Layer, html: string, options: L.TooltipOptions = {}, restMs = AREA_TOOLTIP_REST_MS): void {
    unbindAreaTooltip(layer);
    const tooltip = L.tooltip({ direction: "top", ...options }).setContent(html);

    let timer: ReturnType<typeof setTimeout> | undefined;
    const hide = (): void => {
        clearTimeout(timer);
        timer = undefined;
        map.closeTooltip(tooltip);
    };
    const rest = (event: L.LeafletMouseEvent): void => {
        hide();
        timer = setTimeout(() => {
            if (!map.hasLayer(layer) || isMapContextMenuOpen()) return;
            tooltip.setLatLng(event.latlng);
            map.openTooltip(tooltip);
        }, restMs);
    };
    const handlers: L.LeafletEventHandlerFnMap = { mousemove: rest, mouseout: hide, mousedown: hide, click: hide, contextmenu: hide, remove: hide };
    layer.on(handlers);
    map.on("movestart", hide);
    unbinders.set(layer, () => {
        hide();
        layer.off(handlers);
        map.off("movestart", hide);
    });
}

/** Removes a label bound by {@link bindAreaTooltip}, if there is one. */
export function unbindAreaTooltip(layer: L.Layer): void {
    unbinders.get(layer)?.();
    unbinders.delete(layer);
}
