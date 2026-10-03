/**
 * Hover labels for map areas. A label that followed the pointer would never leave a polygon that fills the view, so
 * an area's label shows only once the pointer rests on it, and hides as soon as it moves or a map menu opens.
 */

import { isMapContextMenuOpen } from "./map-context-menu";

/** How long the pointer has to rest on an area before its label shows. */
export const AREA_TOOLTIP_REST_MS = 600;

const unbinders = new WeakMap<L.Layer, () => void>();

/**
 * Binds a label that shows where the pointer rests on ``layer``.
 * @param map - The map the layer is on; its pans and zooms hide the label.
 * @param layer - The area: a polygon, circle or line.
 * @param html - The label's escaped HTML.
 * @param options - Leaflet tooltip options; it is always opened by hand, so ``permanent`` and ``sticky`` are overridden.
 * @param restMs - How long the pointer has to rest.
 */
export function bindAreaTooltip(map: L.Map, layer: L.Layer, html: string, options: L.TooltipOptions = {}, restMs = AREA_TOOLTIP_REST_MS): void {
    unbindAreaTooltip(layer);
    // Permanent, so Leaflet attaches none of its own hover handlers; it opens one as soon as it is bound or added.
    layer.bindTooltip(html, { direction: "top", ...options, permanent: true, sticky: false });
    layer.closeTooltip();

    let timer: ReturnType<typeof setTimeout> | undefined;
    const hide = (): void => {
        clearTimeout(timer);
        timer = undefined;
        layer.closeTooltip();
    };
    const rest = (event: L.LeafletMouseEvent): void => {
        hide();
        timer = setTimeout(() => {
            if (!isMapContextMenuOpen()) layer.openTooltip(event.latlng);
        }, restMs);
    };
    const handlers: L.LeafletEventHandlerFnMap = { add: hide, mousemove: rest, mouseout: hide, mousedown: hide, click: hide, contextmenu: hide };
    layer.on(handlers);
    map.on("movestart", hide);
    unbinders.set(layer, () => {
        hide();
        layer.off(handlers);
        map.off("movestart", hide);
        layer.unbindTooltip();
    });
}

/** Removes a label bound by {@link bindAreaTooltip}, if there is one. */
export function unbindAreaTooltip(layer: L.Layer): void {
    unbinders.get(layer)?.();
    unbinders.delete(layer);
}
