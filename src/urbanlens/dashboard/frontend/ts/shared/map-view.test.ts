/**
 * The view façade's job is to let one call site drive either engine, so the interesting failures
 * are the asymmetries: coordinate order, bounds corner order, where a pointer event's pixels are
 * measured from, and whether `off` can actually detach a handler it wrapped on the way in.
 */
import { describe, expect, test } from "bun:test";

import { boundsContain, boundsOf, boundsToBBoxString, createLeafletMapView, isMaplibreMap, type MapViewBounds } from "./map-view";
import { asMaplibreView, createMaplibreMapView, type TrackedPopup } from "./maplibre-view";

const BOX: MapViewBounds = { south: 42.3, west: -71.1, north: 42.4, east: -71.0 };

describe("boundsContain", () => {
    test("accepts a point inside", () => {
        expect(boundsContain(BOX, { lat: 42.35, lng: -71.05 })).toBe(true);
    });

    test("accepts a point exactly on an edge, as both engines do", () => {
        expect(boundsContain(BOX, { lat: 42.3, lng: -71.05 })).toBe(true);
        expect(boundsContain(BOX, { lat: 42.35, lng: -71.0 })).toBe(true);
    });

    test("rejects a point outside in either axis", () => {
        expect(boundsContain(BOX, { lat: 42.5, lng: -71.05 })).toBe(false);
        expect(boundsContain(BOX, { lat: 42.35, lng: -70.9 })).toBe(false);
    });
});

describe("boundsToBBoxString", () => {
    test("writes west,south,east,north - the order this app's own bbox parameters use", () => {
        expect(boundsToBBoxString(BOX)).toBe("-71.1,42.3,-71,42.4");
    });
});

describe("boundsOf", () => {
    test("covers every position given", () => {
        expect(boundsOf([{ lat: 1, lng: 2 }, { lat: -3, lng: 8 }, { lat: 0, lng: 0 }])).toEqual({ south: -3, north: 1, west: 0, east: 8 });
    });

    test("is a degenerate box for a single position, not null", () => {
        expect(boundsOf([{ lat: 5, lng: 6 }])).toEqual({ south: 5, north: 5, west: 6, east: 6 });
    });

    test("answers null for nothing, so a caller cannot fit to an empty box", () => {
        expect(boundsOf([])).toBeNull();
    });
});

describe("isMaplibreMap", () => {
    test("recognises a map that can set a layout property", () => {
        expect(isMaplibreMap({ setLayoutProperty: () => undefined })).toBe(true);
    });

    test("rejects a Leaflet map, which has no such method", () => {
        expect(isMaplibreMap({ setView: () => undefined, eachLayer: () => undefined })).toBe(false);
    });
});

// -- Stubs -------------------------------------------------------------------------------------

function container(rect: { left: number; top: number }): HTMLElement {
    const element = document.createElement("div");
    element.getBoundingClientRect = (): DOMRect => ({ left: rect.left, top: rect.top, right: 0, bottom: 0, x: rect.left, y: rect.top, width: 0, height: 0, toJSON: () => ({}) }) as DOMRect;
    return element;
}

interface Listener {
    type: string;
    fn: (event: unknown) => void;
    once: boolean;
}

function leafletMapStub() {
    const listeners: Listener[] = [];
    const calls: Record<string, unknown[]> = {};
    const record = (name: string, value: unknown): void => void (calls[name] = [...(calls[name] ?? []), value]);
    const element = container({ left: 10, top: 20 });
    const map = {
        listeners,
        calls,
        setView: (center: [number, number], zoom?: number) => record("setView", { center, zoom }),
        getCenter: () => ({ lat: 42.36, lng: -71.06 }),
        getZoom: () => 13,
        getMinZoom: () => 2,
        getMaxZoom: () => 21,
        getBounds: () => ({ getSouth: () => BOX.south, getWest: () => BOX.west, getNorth: () => BOX.north, getEast: () => BOX.east }),
        fitBounds: (bounds: unknown, options: unknown) => record("fitBounds", { bounds, options }),
        getContainer: () => element,
        latLngToContainerPoint: (value: [number, number]) => ({ x: value[1], y: value[0] }),
        mouseEventToLatLng: (event: MouseEvent) => ({ lat: event.clientY, lng: event.clientX }),
        dragging: { enable: () => record("dragging", true), disable: () => record("dragging", false) },
        invalidateSize: () => record("invalidateSize", true),
        closePopup: () => record("closePopup", true),
        on: (type: string, fn: (event: unknown) => void) => void listeners.push({ type, fn, once: false }),
        once: (type: string, fn: (event: unknown) => void) => void listeners.push({ type, fn, once: true }),
        off: (type: string, fn: (event: unknown) => void) => {
            const index = listeners.findIndex((listener) => listener.type === type && listener.fn === fn);
            if (index >= 0) listeners.splice(index, 1);
        },
    };
    return map;
}

function maplibreMapStub() {
    const listeners: Listener[] = [];
    const calls: Record<string, unknown[]> = {};
    const record = (name: string, value: unknown): void => void (calls[name] = [...(calls[name] ?? []), value]);
    const element = container({ left: 10, top: 20 });
    const map = {
        listeners,
        calls,
        jumpTo: (options: unknown) => record("jumpTo", options),
        getCenter: () => ({ lat: 42.36, lng: -71.06 }),
        getZoom: () => 13,
        getMinZoom: () => 2,
        getMaxZoom: () => 21,
        getBounds: () => ({ getSouth: () => BOX.south, getWest: () => BOX.west, getNorth: () => BOX.north, getEast: () => BOX.east }),
        fitBounds: (bounds: unknown, options: unknown) => record("fitBounds", { bounds, options }),
        getContainer: () => element,
        project: (value: [number, number]) => ({ x: value[0], y: value[1] }),
        unproject: (value: [number, number]) => ({ lat: value[1], lng: value[0] }),
        dragPan: { enable: () => record("dragPan", true), disable: () => record("dragPan", false) },
        resize: () => record("resize", true),
        on: (type: string, fn: (event: unknown) => void) => void listeners.push({ type, fn, once: false }),
        once: (type: string, fn: (event: unknown) => void) => void listeners.push({ type, fn, once: true }),
        off: (type: string, fn: (event: unknown) => void) => {
            const index = listeners.findIndex((listener) => listener.type === type && listener.fn === fn);
            if (index >= 0) listeners.splice(index, 1);
        },
    };
    return map;
}

type LeafletStubMap = ReturnType<typeof leafletMapStub>;
type MaplibreStubMap = ReturnType<typeof maplibreMapStub>;

function leafletView(): { view: ReturnType<typeof createLeafletMapView>; map: LeafletStubMap } {
    const map = leafletMapStub();
    return { view: createLeafletMapView(map as unknown as L.Map), map };
}

function maplibreView(): { view: ReturnType<typeof createMaplibreMapView>; map: MaplibreStubMap } {
    const map = maplibreMapStub();
    return { view: createMaplibreMapView(map as unknown as import("maplibre-gl").Map), map };
}

function deliver(map: LeafletStubMap | MaplibreStubMap, type: string, event: unknown): void {
    for (const listener of [...map.listeners]) if (listener.type === type) listener.fn(event);
}

// -- The shared contract -------------------------------------------------------------------------

const VIEWS = [
    { name: "leaflet", build: leafletView as () => { view: ReturnType<typeof createLeafletMapView>; map: LeafletStubMap | MaplibreStubMap } },
    { name: "maplibre", build: maplibreView as () => { view: ReturnType<typeof createLeafletMapView>; map: LeafletStubMap | MaplibreStubMap } },
];

for (const engine of VIEWS) {
    describe(`view contract (${engine.name})`, () => {
        test("reports its centre as lat/lng, whichever order the engine stores", () => {
            const { view } = engine.build();
            expect(view.getCenter()).toEqual({ lat: 42.36, lng: -71.06 });
        });

        test("reports the zoom range", () => {
            const { view } = engine.build();
            expect([view.getZoom(), view.getMinZoom(), view.getMaxZoom()]).toEqual([13, 2, 21]);
        });

        test("reports bounds as the four named edges", () => {
            const { view } = engine.build();
            expect(view.getBounds()).toEqual(BOX);
        });

        test("hands back the engine's own container", () => {
            const { view, map } = engine.build();
            expect(view.getContainer()).toBe(map.getContainer());
        });

        test("detaches a handler that was attached", () => {
            // Each wrapper is built on the way in, so `off` can only work if it finds the same one.
            const { view, map } = engine.build();
            const handler = (): void => undefined;
            view.on("moveend", handler);
            expect(map.listeners).toHaveLength(1);
            view.off("moveend", handler);
            expect(map.listeners).toHaveLength(0);
        });

        test("keeps one wrapper per handler and event, not one per call", () => {
            const { view, map } = engine.build();
            const handler = (): void => undefined;
            view.on("moveend", handler);
            view.on("zoomend", handler);
            expect(new Set(map.listeners.map((listener) => listener.fn)).size).toBe(2);
            view.off("moveend", handler);
            expect(map.listeners.map((listener) => listener.type)).toEqual(["zoomend"]);
        });

        test("leaves an unrelated handler attached when one is detached", () => {
            const { view, map } = engine.build();
            const kept = (): void => undefined;
            const dropped = (): void => undefined;
            view.on("moveend", kept);
            view.on("moveend", dropped);
            view.off("moveend", dropped);
            expect(map.listeners).toHaveLength(1);
        });

        test("delivers a click with the geography resolved and the original event kept", () => {
            const { view, map } = engine.build();
            const seen: { lat: number; lng: number; type: string }[] = [];
            view.on("click", (event) => seen.push({ lat: event.lat, lng: event.lng, type: event.originalEvent.type }));
            const raw = new MouseEvent("click");
            deliver(map, "click", { lngLat: { lat: 1, lng: 2 }, latlng: { lat: 1, lng: 2 }, originalEvent: raw });
            expect(seen).toEqual([{ lat: 1, lng: 2, type: "click" }]);
        });

        test("delivers a payload-free event without inventing one", () => {
            const { view, map } = engine.build();
            const seen: unknown[] = [];
            view.on("moveend", (payload) => seen.push(payload));
            deliver(map, "moveend", { type: "moveend" });
            expect(seen).toEqual([undefined]);
        });

        test("turns dragging off and on again", () => {
            const { view, map } = engine.build();
            view.setDraggingEnabled(false);
            view.setDraggingEnabled(true);
            expect(map.calls.dragging ?? map.calls.dragPan).toEqual([false, true]);
        });
    });
}

// -- Engine specifics ----------------------------------------------------------------------------

describe("Leaflet view", () => {
    test("sets the view in Leaflet's lat,lng order", () => {
        const { view, map } = leafletView();
        view.setView({ lat: 42.36, lng: -71.06 }, 15);
        expect(map.calls.setView).toEqual([{ center: [42.36, -71.06], zoom: 15 }]);
    });

    test("fits bounds as south-west then north-east corners", () => {
        const { view, map } = leafletView();
        view.fitBounds(BOX, { padding: 40, maxZoom: 14 });
        expect(map.calls.fitBounds).toEqual([
            {
                bounds: [
                    [42.3, -71.1],
                    [42.4, -71.0],
                ],
                options: { padding: [40, 40], maxZoom: 14 },
            },
        ]);
    });

    test("passes per-corner padding through as Leaflet's own two options", () => {
        const { view, map } = leafletView();
        view.fitBounds(BOX, { padding: { topLeft: [10, 20], bottomRight: [30, 40] } });
        expect((map.calls.fitBounds?.[0] as { options: Record<string, unknown> }).options).toEqual({ paddingTopLeft: [10, 20], paddingBottomRight: [30, 40], maxZoom: undefined });
    });

    test("resolves a pointer event through Leaflet, which takes page coordinates", () => {
        const { view } = leafletView();
        const raw = new MouseEvent("click", { clientX: 100, clientY: 200 });
        expect(view.pointerToLatLng(raw)).toEqual({ lat: 200, lng: 100 });
    });

    test("reads a popup's element off the event Leaflet fires", () => {
        const { view, map } = leafletView();
        const element = document.createElement("div");
        const seen: (HTMLElement | null)[] = [];
        view.on("popupopen", (event) => seen.push(event.element));
        deliver(map, "popupopen", { popup: { getElement: () => element } });
        expect(seen).toEqual([element]);
    });

    test("invalidates its size when the page reports a resize", () => {
        const { view, map } = leafletView();
        view.resized();
        expect(map.calls.invalidateSize).toEqual([true]);
    });

    test("is not a MapLibre view", () => {
        const { view } = leafletView();
        expect(asMaplibreView(view)).toBeNull();
    });
});

describe("MapLibre view", () => {
    test("sets the view in MapLibre's lng,lat order", () => {
        const { view, map } = maplibreView();
        view.setView({ lat: 42.36, lng: -71.06 }, 15);
        expect(map.calls.jumpTo).toEqual([{ center: [-71.06, 42.36], zoom: 15 }]);
    });

    test("omits zoom entirely when none was asked for, rather than sending undefined", () => {
        const { view, map } = maplibreView();
        view.setView({ lat: 1, lng: 2 });
        expect(map.calls.jumpTo).toEqual([{ center: [2, 1] }]);
    });

    test("fits bounds as west-south then east-north corners", () => {
        const { view, map } = maplibreView();
        view.fitBounds(BOX, { padding: 40, maxZoom: 14 });
        expect(map.calls.fitBounds).toEqual([
            {
                bounds: [
                    [-71.1, 42.3],
                    [-71.0, 42.4],
                ],
                options: { padding: 40, maxZoom: 14, animate: false },
            },
        ]);
    });

    test("does not animate a fit, matching what Leaflet's default already did", () => {
        const { view, map } = maplibreView();
        view.fitBounds(BOX);
        expect((map.calls.fitBounds?.[0] as { options: { animate: boolean } }).options.animate).toBe(false);
    });

    test("spells per-corner padding as MapLibre's four named edges", () => {
        const { view, map } = maplibreView();
        view.fitBounds(BOX, { padding: { topLeft: [10, 20], bottomRight: [30, 40] } });
        expect((map.calls.fitBounds?.[0] as { options: { padding: unknown } }).options.padding).toEqual({ left: 10, top: 20, right: 30, bottom: 40 });
    });

    test("measures a pointer event from the container, not the page", () => {
        // MapLibre's unproject wants container pixels; handing it page pixels silently offsets
        // every right-click by wherever the map sits on the page.
        const { view } = maplibreView();
        const raw = new MouseEvent("click", { clientX: 100, clientY: 200 });
        expect(view.pointerToLatLng(raw)).toEqual({ lat: 180, lng: 90 });
    });

    test("projects a coordinate to container pixels in lng,lat order", () => {
        const { view } = maplibreView();
        expect(view.latLngToContainerPoint({ lat: 5, lng: 9 })).toEqual({ x: 9, y: 5 });
    });

    test("closes every popup it was given, which no MapLibre map does itself", () => {
        const { view } = maplibreView();
        const closed: string[] = [];
        const popup = (id: string, open: boolean): TrackedPopup => ({ isOpen: () => open, close: () => void closed.push(id), element: () => null });
        view.trackPopup(popup("a", true));
        view.trackPopup(popup("b", false));
        view.closePopup();
        expect(closed).toEqual(["a"]);
    });

    test("stops closing a popup once it is untracked", () => {
        const { view } = maplibreView();
        let closed = 0;
        const stop = view.trackPopup({ isOpen: () => true, close: () => (closed += 1), element: () => null });
        stop();
        view.closePopup();
        expect(closed).toBe(0);
    });

    test("fires popupopen with the popup's element when one opens", () => {
        const { view } = maplibreView();
        const element = document.createElement("div");
        const seen: (HTMLElement | null)[] = [];
        view.on("popupopen", (event) => seen.push(event.element));
        view.popupOpened({ isOpen: () => true, close: () => undefined, element: () => element });
        expect(seen).toEqual([element]);
    });

    test("fires a one-shot popupopen listener once only", () => {
        const { view } = maplibreView();
        let fired = 0;
        view.once("popupopen", () => (fired += 1));
        const popup: TrackedPopup = { isOpen: () => true, close: () => undefined, element: () => null };
        view.popupOpened(popup);
        view.popupOpened(popup);
        expect(fired).toBe(1);
    });

    test("can detach a popupopen listener, which is not one of MapLibre's own events", () => {
        const { view } = maplibreView();
        let fired = 0;
        const handler = (): void => void (fired += 1);
        view.on("popupopen", handler);
        view.off("popupopen", handler);
        view.popupOpened({ isOpen: () => true, close: () => undefined, element: () => null });
        expect(fired).toBe(0);
    });

    test("resizes rather than invalidating, which MapLibre has no notion of", () => {
        const { view, map } = maplibreView();
        view.resized();
        expect(map.calls.resize).toEqual([true]);
    });

    test("narrows to the MapLibre view for the call sites that need popup tracking", () => {
        const { view } = maplibreView();
        expect(asMaplibreView(view)).toBe(view);
    });
});
