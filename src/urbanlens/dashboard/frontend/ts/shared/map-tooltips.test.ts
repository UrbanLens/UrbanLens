/**
 * An area's hover label shows once the pointer rests on it, never follows the pointer, and stays shut while a map menu is open.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { bindAreaTooltip, unbindAreaTooltip } from "./map-tooltips";

declare const L: typeof import("leaflet");

const REST_MS = 15;
const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));
const realL = (globalThis as Record<string, unknown>).L;

let map: L.Map;
let polygon: L.Polygon;

function labelShown(): boolean {
    return document.querySelector(".leaflet-tooltip.area-label") !== null;
}

function hover(lat = 41.733, lng = -73.928): void {
    polygon.fire("mousemove", { latlng: L.latLng(lat, lng) });
}

function bind(): void {
    bindAreaTooltip(map, polygon, "Property boundary", { className: "area-label" }, REST_MS);
}

beforeAll(async () => {
    (globalThis as Record<string, unknown>).L = (await import("leaflet")).default;
});

afterAll(() => {
    (globalThis as Record<string, unknown>).L = realL;
});

beforeEach(() => {
    document.body.innerHTML = '<div id="map" style="width: 400px; height: 300px"></div>';
    map = L.map("map", { fadeAnimation: false, zoomAnimation: false }).setView([41.733, -73.928], 17);
    polygon = L.polygon([
        [41.72, -73.94],
        [41.74, -73.94],
        [41.74, -73.91],
        [41.72, -73.91],
    ]).addTo(map);
});

afterEach(() => {
    unbindAreaTooltip(polygon);
    map.remove();
});

describe("bindAreaTooltip", () => {
    test("binds shut, and opens only once the pointer rests", async () => {
        bind();
        expect(labelShown()).toBe(false);

        hover();
        expect(labelShown()).toBe(false);

        await sleep(REST_MS * 3);
        expect(labelShown()).toBe(true);
    });

    test("does not follow the pointer: moving hides it and starts the wait again", async () => {
        bind();
        hover();
        await sleep(REST_MS * 3);
        expect(labelShown()).toBe(true);

        hover(41.734, -73.929);
        expect(labelShown()).toBe(false);
        await sleep(REST_MS * 3);
        expect(labelShown()).toBe(true);
    });

    test("never opens while a map menu is open", async () => {
        bind();
        document.body.insertAdjacentHTML("beforeend", '<div class="map-context-menu"></div>');

        hover();
        await sleep(REST_MS * 3);

        expect(labelShown()).toBe(false);
    });

    test("a click, a right-click, leaving the area, the map moving or the layer going hides it", async () => {
        const hiders: [string, () => void][] = [
            ["click", () => polygon.fire("click", { latlng: L.latLng(41.733, -73.928) })],
            ["contextmenu", () => polygon.fire("contextmenu", { latlng: L.latLng(41.733, -73.928) })],
            ["mouseout", () => polygon.fire("mouseout")],
            ["movestart", () => map.fire("movestart")],
            ["remove", () => map.removeLayer(polygon)],
        ];
        for (const [name, hide] of hiders) {
            if (!map.hasLayer(polygon)) polygon.addTo(map);
            bind();
            hover();
            await sleep(REST_MS * 3);
            expect(labelShown()).toBe(true);

            hide();

            expect([name, labelShown()]).toEqual([name, false]);
        }
    });

    test("a click before the wait is up cancels it", async () => {
        bind();
        hover();
        polygon.fire("click", { latlng: L.latLng(41.733, -73.928) });
        await sleep(REST_MS * 3);

        expect(labelShown()).toBe(false);
    });

    test("re-adding the layer mid-drag does not open it once the drag ends", () => {
        bind();
        // Leaflet defers a bound tooltip's add-time open to moveend while the map is being dragged.
        const dragging = map.dragging as L.Handler & { moving: () => boolean };
        const moving = dragging.moving;
        dragging.moving = () => true;

        map.removeLayer(polygon);
        polygon.addTo(map);
        dragging.moving = moving;
        map.fire("moveend");

        expect(labelShown()).toBe(false);
    });

    test("rebinding replaces the label, and unbinding removes it", async () => {
        bind();
        bind();
        hover();
        await sleep(REST_MS * 3);
        expect(document.querySelectorAll(".leaflet-tooltip.area-label").length).toBe(1);

        unbindAreaTooltip(polygon);
        expect(labelShown()).toBe(false);
        hover();
        await sleep(REST_MS * 3);
        expect(labelShown()).toBe(false);
    });
});
