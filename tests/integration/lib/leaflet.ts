/**
 * Reaching a page's own Leaflet map. Nothing registers the instances, so an init script wraps
 * `L.Map.prototype.initialize` and records each map as it is built.
 */

import type { Page } from "@playwright/test";

const REGISTRY = "__ulLeafletMaps";

/** Records every Leaflet map the page builds on `window.__ulLeafletMaps`. Call before navigating. */
export async function installLeafletMapCapture(page: Page): Promise<void> {
    await page.addInitScript((registry) => {
        const w = window as unknown as Record<string, unknown> & { L?: { Map?: { prototype?: { initialize?: (...args: unknown[]) => unknown } } } };
        const iv = window.setInterval(() => {
            const proto = w.L?.Map?.prototype;
            if (!proto?.initialize) return; // Leaflet loads after this script runs.
            window.clearInterval(iv);
            const original = proto.initialize;
            proto.initialize = function (this: unknown, ...args: unknown[]) {
                const result = original.apply(this, args);
                const maps = (w[registry] as unknown[] | undefined) ?? [];
                maps.push(this);
                w[registry] = maps;
                return result;
            };
        }, 10);
    }, REGISTRY);
}

/**
 * Coordinates of every pin marker held by the page's first map, clustered or not.
 *
 * A cluster group's `getLayers()` holds its markers whether or not they are currently drawn inside a
 * cluster bubble, so this is the map's pin set rather than what happens to be unclustered at this zoom.
 * The cluster bubbles themselves are skipped.
 */
export async function markerLatLngs(page: Page): Promise<Array<{ lat: number; lng: number }>> {
    return page.evaluate((registry) => {
        type Layer = {
            getLatLng?: () => { lat: number; lng: number };
            getLayers?: () => Layer[];
            getChildCount?: () => number;
        };
        const w = window as unknown as Record<string, unknown> & { L: { Marker: new (...args: never[]) => unknown } };
        const map = (w[registry] as Array<{ eachLayer: (fn: (layer: Layer) => void) => void }> | undefined)?.[0];
        if (!map) throw new Error("no Leaflet map was captured; call installLeafletMapCapture before navigating");
        const seen = new Set<Layer>();
        const out: Array<{ lat: number; lng: number }> = [];
        const visit = (layer: Layer): void => {
            if (seen.has(layer)) return;
            seen.add(layer);
            if (layer instanceof w.L.Marker && typeof layer.getChildCount !== "function" && layer.getLatLng) {
                const { lat, lng } = layer.getLatLng();
                out.push({ lat, lng });
            }
            layer.getLayers?.().forEach(visit);
        };
        map.eachLayer(visit);
        return out;
    }, REGISTRY);
}
