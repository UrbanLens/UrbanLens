/**
 * The marker façade's claim is that a call site written once behaves the same on either engine, so
 * most of what follows is one contract suite run twice rather than two suites. The engine-specific
 * blocks below it cover the things only one engine can get wrong - chiefly that MapLibre's marker,
 * which cannot exist without an element, must not exist until the pin is actually shown.
 */
import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { createLeafletMarker, stopPointerEvent, type MapMarker, type MapMarkerOptions, type MarkerIcon } from "./map-markers";
import { createMaplibreMarker } from "./maplibre-markers";
import type { LatLng, MapView } from "./map-view";
import type { MaplibreMapView, TrackedPopup } from "./maplibre-view";

const ICON: MarkerIcon = { html: "<span>pin</span>", className: "map-pin-icon-wrap", size: [30, 40], anchor: [15, 40] };
const AT: LatLng = { lat: 42.3601, lng: -71.0589 };

// -- Leaflet stub ------------------------------------------------------------------------------

interface StubDivIcon {
    __kind: "divIcon";
    options: { className: string; html: string; iconSize: [number, number]; iconAnchor: [number, number] };
}

interface StubLeafletMarker {
    latlng: [number, number];
    options: Record<string, unknown>;
    icon: StubDivIcon | null;
    popupHtml: string | null;
    popupOpen: boolean;
    handlers: Record<string, ((event: unknown) => void)[]>;
    onMap: object | null;
    dragging: { enabled: boolean; enable(): void; disable(): void };
    element: HTMLElement | null;
    getLatLng(): { lat: number; lng: number };
    setLatLng(value: [number, number]): void;
    setIcon(icon: StubDivIcon): void;
    bindPopup(html: string): void;
    openPopup(): void;
    closePopup(): void;
    getElement(): HTMLElement | null;
    on(type: string, fn: (event: unknown) => void): void;
    addTo(map: object): void;
    remove(): void;
}

interface LeafletStub {
    markers: StubLeafletMarker[];
    icons: StubDivIcon[];
    map: { hasLayer(layer: object): boolean };
}

const realL = (globalThis as Record<string, unknown>).L;
const realMaplibregl = (globalThis as Record<string, unknown>).maplibregl;

function stubLeaflet(): LeafletStub {
    const state: LeafletStub = {
        markers: [],
        icons: [],
        map: { hasLayer: (layer: object) => state.markers.some((marker) => marker === layer && marker.onMap !== null) },
    };
    const divIcon = (options: StubDivIcon["options"]): StubDivIcon => {
        const icon: StubDivIcon = { __kind: "divIcon", options };
        state.icons.push(icon);
        return icon;
    };
    const marker = (latlng: [number, number], options: Record<string, unknown> = {}): StubLeafletMarker => {
        const made: StubLeafletMarker = {
            latlng,
            options,
            icon: (options.icon as StubDivIcon | undefined) ?? null,
            popupHtml: null,
            popupOpen: false,
            handlers: {},
            onMap: null,
            element: null,
            dragging: {
                enabled: options.draggable === true,
                enable() {
                    this.enabled = true;
                },
                disable() {
                    this.enabled = false;
                },
            },
            getLatLng: () => ({ lat: made.latlng[0], lng: made.latlng[1] }),
            setLatLng: (value) => void (made.latlng = value),
            setIcon: (icon) => void (made.icon = icon),
            bindPopup: (html) => void (made.popupHtml = html),
            openPopup: () => void (made.popupOpen = true),
            closePopup: () => void (made.popupOpen = false),
            // Leaflet builds the icon in onAdd, so there is nothing to return until then.
            getElement: () => made.element,
            on: (type, fn) => void (made.handlers[type] = [...(made.handlers[type] ?? []), fn]),
            addTo: (map) => {
                made.onMap = map;
                made.element = document.createElement("div");
            },
            remove: () => {
                made.onMap = null;
                made.element = null;
            },
        };
        state.markers.push(made);
        return made;
    };
    (globalThis as Record<string, unknown>).L = { marker, divIcon };
    return state;
}

// -- MapLibre stub -----------------------------------------------------------------------------

interface StubMaplibrePopup {
    html: string | null;
    lngLat: [number, number] | null;
    offset: [number, number] | null;
    options: Record<string, unknown>;
    open: boolean;
    element: HTMLElement;
    setHTML(html: string): StubMaplibrePopup;
    setLngLat(value: [number, number]): StubMaplibrePopup;
    setOffset(value: [number, number]): StubMaplibrePopup;
    addTo(map: object): StubMaplibrePopup;
    remove(): StubMaplibrePopup;
    isOpen(): boolean;
    getElement(): HTMLElement;
}

interface StubMaplibreMarker {
    options: Record<string, unknown>;
    element: HTMLElement;
    lngLat: [number, number] | null;
    offset: [number, number] | null;
    draggable: boolean;
    onMap: object | null;
    handlers: Record<string, (() => void)[]>;
    setLngLat(value: [number, number]): StubMaplibreMarker;
    getLngLat(): { lat: number; lng: number };
    setOffset(value: [number, number]): StubMaplibreMarker;
    setDraggable(value: boolean): StubMaplibreMarker;
    addTo(map: object): StubMaplibreMarker;
    remove(): StubMaplibreMarker;
    on(type: string, fn: () => void): StubMaplibreMarker;
}

interface MaplibreStub {
    markers: StubMaplibreMarker[];
    popups: StubMaplibrePopup[];
}

function stubMaplibre(): MaplibreStub {
    const state: MaplibreStub = { markers: [], popups: [] };

    class Marker implements StubMaplibreMarker {
        options: Record<string, unknown>;
        element: HTMLElement;
        lngLat: [number, number] | null = null;
        offset: [number, number] | null = null;
        draggable: boolean;
        onMap: object | null = null;
        handlers: Record<string, (() => void)[]> = {};

        constructor(options: Record<string, unknown> = {}) {
            // MapLibre has no marker without an element; a stub that invented one would hide the
            // very thing the laziness tests below are checking.
            if (!(options.element instanceof HTMLElement)) throw new Error("maplibregl.Marker needs an element");
            this.options = options;
            this.element = options.element;
            this.offset = (options.offset as [number, number] | undefined) ?? null;
            this.draggable = options.draggable === true;
            state.markers.push(this);
        }
        setLngLat(value: [number, number]): this {
            this.lngLat = value;
            return this;
        }
        getLngLat(): { lat: number; lng: number } {
            return { lat: this.lngLat?.[1] ?? 0, lng: this.lngLat?.[0] ?? 0 };
        }
        setOffset(value: [number, number]): this {
            this.offset = value;
            return this;
        }
        setDraggable(value: boolean): this {
            this.draggable = value;
            return this;
        }
        addTo(map: object): this {
            this.onMap = map;
            return this;
        }
        remove(): this {
            this.onMap = null;
            return this;
        }
        on(type: string, fn: () => void): this {
            this.handlers[type] = [...(this.handlers[type] ?? []), fn];
            return this;
        }
        fire(type: string): void {
            for (const handler of this.handlers[type] ?? []) handler();
        }
    }

    class Popup implements StubMaplibrePopup {
        html: string | null = null;
        lngLat: [number, number] | null = null;
        offset: [number, number] | null;
        options: Record<string, unknown>;
        open = false;
        element = document.createElement("div");

        constructor(options: Record<string, unknown> = {}) {
            this.options = options;
            this.offset = (options.offset as [number, number] | undefined) ?? null;
            state.popups.push(this);
        }
        setHTML(html: string): this {
            this.html = html;
            return this;
        }
        setLngLat(value: [number, number]): this {
            this.lngLat = value;
            return this;
        }
        setOffset(value: [number, number]): this {
            this.offset = value;
            return this;
        }
        addTo(): this {
            this.open = true;
            return this;
        }
        remove(): this {
            this.open = false;
            return this;
        }
        isOpen(): boolean {
            return this.open;
        }
        getElement(): HTMLElement {
            return this.element;
        }
    }

    (globalThis as Record<string, unknown>).maplibregl = { Marker, Popup };
    return state;
}

/** A `MaplibreMapView` with only what a marker touches, plus a record of what it was told. */
function fakeMaplibreView(): MaplibreMapView & { tracked: TrackedPopup[]; opened: TrackedPopup[]; native: object } {
    const tracked: TrackedPopup[] = [];
    const opened: TrackedPopup[] = [];
    const view = {
        kind: "maplibre" as const,
        native: { id: "maplibre-map" },
        tracked,
        opened,
        trackPopup: (popup: TrackedPopup) => {
            tracked.push(popup);
            return () => void tracked.splice(tracked.indexOf(popup), 1);
        },
        popupOpened: (popup: TrackedPopup) => void opened.push(popup),
    };
    return view as unknown as MaplibreMapView & { tracked: TrackedPopup[]; opened: TrackedPopup[]; native: object };
}

// -- The shared contract, run on both engines ---------------------------------------------------

interface Engine {
    name: string;
    /** Builds a marker plus whatever stand-in for a map this engine's `addTo` wants. */
    create(options?: MapMarkerOptions): { marker: MapMarker; view: MapView };
    /** Makes the engine deliver a pointer event of this kind to the marker, and returns it. */
    firePointer(marker: MapMarker, event: "click" | "contextmenu"): MouseEvent;
    /** Whether a popup is currently shown for this marker. */
    popupIsOpen(marker: MapMarker): boolean;
}

let leaflet: LeafletStub;
let maplibre: MaplibreStub;

beforeEach(() => {
    leaflet = stubLeaflet();
    maplibre = stubMaplibre();
});

afterEach(() => {
    if (realL === undefined) delete (globalThis as Record<string, unknown>).L;
    else (globalThis as Record<string, unknown>).L = realL;
    if (realMaplibregl === undefined) delete (globalThis as Record<string, unknown>).maplibregl;
    else (globalThis as Record<string, unknown>).maplibregl = realMaplibregl;
});

/** The stub Leaflet marker behind a façade - found by identity, since the façade keeps it private. */
function leafletBehind(index = 0): StubLeafletMarker {
    const marker = leaflet.markers[index];
    if (!marker) throw new Error("no Leaflet marker was built");
    return marker;
}

const ENGINES: Engine[] = [
    {
        name: "leaflet",
        create: (options) => {
            const marker = createLeafletMarker(AT, options);
            const view = { kind: "leaflet", native: leaflet.map } as unknown as MapView;
            return { marker, view };
        },
        firePointer: (_marker, event) => {
            const raw = pointerEvent(event);
            const behind = leafletBehind(leaflet.markers.length - 1);
            for (const handler of behind.handlers[event] ?? []) handler({ latlng: { lat: AT.lat, lng: AT.lng }, originalEvent: raw });
            return raw;
        },
        popupIsOpen: () => leafletBehind(leaflet.markers.length - 1).popupOpen,
    },
    {
        name: "maplibre",
        create: (options) => {
            const marker = createMaplibreMarker(AT, options);
            return { marker, view: fakeMaplibreView() as unknown as MapView };
        },
        firePointer: (marker, event) => {
            const element = marker.getElement();
            if (!element) throw new Error("marker has no element to dispatch on");
            const raw = pointerEvent(event);
            element.dispatchEvent(raw);
            return raw;
        },
        popupIsOpen: () => maplibre.popups.some((popup) => popup.isOpen()),
    },
];

function pointerEvent(type: string): MouseEvent {
    return new MouseEvent(type, { bubbles: true, cancelable: true });
}

for (const engine of ENGINES) {
    describe(`marker contract (${engine.name})`, () => {
        test("reports the position it was built at", () => {
            const { marker } = engine.create({ icon: ICON });
            expect(marker.getLatLng()).toEqual(AT);
        });

        test("reports a position set after it was built", () => {
            const { marker } = engine.create({ icon: ICON });
            marker.setLatLng({ lat: 51.5, lng: -0.12 });
            expect(marker.getLatLng()).toEqual({ lat: 51.5, lng: -0.12 });
        });

        test("keeps a position set before it was ever shown", () => {
            const { marker, view } = engine.create({ icon: ICON });
            marker.setLatLng({ lat: 51.5, lng: -0.12 });
            marker.addTo(view);
            expect(marker.getLatLng()).toEqual({ lat: 51.5, lng: -0.12 });
        });

        test("has no element until it is shown", () => {
            const { marker } = engine.create({ icon: ICON });
            expect(marker.getElement()).toBeNull();
            expect(marker.isOnMap()).toBe(false);
        });

        test("has an element once shown", () => {
            const { marker, view } = engine.create({ icon: ICON });
            marker.addTo(view);
            expect(marker.getElement()).not.toBeNull();
            expect(marker.isOnMap()).toBe(true);
        });

        test("gives the element up again when removed", () => {
            const { marker, view } = engine.create({ icon: ICON });
            marker.addTo(view);
            marker.remove();
            expect(marker.getElement()).toBeNull();
            expect(marker.isOnMap()).toBe(false);
        });

        test("removing one that was never shown is not an error", () => {
            const { marker } = engine.create({ icon: ICON });
            expect(() => marker.remove()).not.toThrow();
        });

        test("delivers a click with the geography and the original event", () => {
            const { marker, view } = engine.create({ icon: ICON });
            marker.addTo(view);
            const seen: { lat: number; lng: number; type: string }[] = [];
            marker.on("click", (event) => seen.push({ lat: event.lat, lng: event.lng, type: event.originalEvent.type }));
            engine.firePointer(marker, "click");
            expect(seen).toEqual([{ lat: AT.lat, lng: AT.lng, type: "click" }]);
        });

        test("delivers a right-click separately from a click", () => {
            const { marker, view } = engine.create({ icon: ICON });
            marker.addTo(view);
            const clicks: string[] = [];
            marker.on("click", () => clicks.push("click"));
            marker.on("contextmenu", () => clicks.push("contextmenu"));
            engine.firePointer(marker, "contextmenu");
            expect(clicks).toEqual(["contextmenu"]);
        });

        test("opens a bound popup on demand", () => {
            const { marker, view } = engine.create({ icon: ICON });
            marker.addTo(view);
            marker.bindPopup("<b>hello</b>");
            expect(engine.popupIsOpen(marker)).toBe(false);
            marker.openPopup();
            expect(engine.popupIsOpen(marker)).toBe(true);
        });

        test("closes a popup it opened", () => {
            const { marker, view } = engine.create({ icon: ICON });
            marker.addTo(view);
            marker.bindPopup("<b>hello</b>");
            marker.openPopup();
            marker.closePopup();
            expect(engine.popupIsOpen(marker)).toBe(false);
        });

        test("takes a draggable marker off dragging when told to", () => {
            const { marker, view } = engine.create({ icon: ICON, draggable: true });
            marker.addTo(view);
            marker.setDraggable(false);
            expect(engine.name === "leaflet" ? leafletBehind().dragging.enabled : maplibre.markers[0]?.draggable).toBe(false);
        });
    });
}

// -- Leaflet specifics --------------------------------------------------------------------------

describe("Leaflet marker", () => {
    test("builds its icon as a div icon carrying the size and anchor given", () => {
        const { marker, view } = ENGINES[0]!.create({ icon: ICON });
        marker.addTo(view);
        expect(leaflet.icons[0]?.options).toEqual({ className: "map-pin-icon-wrap", html: "<span>pin</span>", iconSize: [30, 40], iconAnchor: [15, 40] });
    });

    test("does not hand Leaflet its own div-icon chrome when no class was asked for", () => {
        const { marker } = ENGINES[0]!.create({ icon: { html: "x", size: [1, 1], anchor: [0, 0] } });
        marker.setIcon({ html: "y", size: [2, 2], anchor: [1, 1] });
        expect(leaflet.icons.at(-1)?.options.className).toBe("");
    });

    test("passes draggable through to Leaflet's own option", () => {
        ENGINES[0]!.create({ icon: ICON, draggable: true });
        expect(leafletBehind().options.draggable).toBe(true);
    });
});

// -- MapLibre specifics -------------------------------------------------------------------------

describe("MapLibre marker", () => {
    test("builds no marker and no element for a pin that is never shown", () => {
        // The main map holds tens of thousands of these; one element each would be the page load.
        for (let index = 0; index < 50; index += 1) createMaplibreMarker({ lat: index, lng: index }, { icon: ICON });
        expect(maplibre.markers).toHaveLength(0);
    });

    test("builds exactly one marker when shown twice on the same map", () => {
        const { marker, view } = ENGINES[1]!.create({ icon: ICON });
        marker.addTo(view);
        marker.addTo(view);
        expect(maplibre.markers).toHaveLength(1);
    });

    test("anchors by the icon's top-left with the anchor negated, reproducing Leaflet's iconAnchor", () => {
        const { marker, view } = ENGINES[1]!.create({ icon: ICON });
        marker.addTo(view);
        expect(maplibre.markers[0]?.options.anchor).toBe("top-left");
        expect(maplibre.markers[0]?.offset).toEqual([-15, -40]);
    });

    test("sizes the element to the icon, which MapLibre will not do itself", () => {
        const { marker, view } = ENGINES[1]!.create({ icon: ICON });
        marker.addTo(view);
        const element = marker.getElement()!;
        expect([element.style.width, element.style.height]).toEqual(["30px", "40px"]);
        expect(element.className).toBe("map-pin-icon-wrap");
        expect(element.innerHTML).toBe("<span>pin</span>");
    });

    test("replays an icon set before it was ever shown", () => {
        const { marker, view } = ENGINES[1]!.create();
        marker.setIcon(ICON);
        marker.addTo(view);
        expect(marker.getElement()?.innerHTML).toBe("<span>pin</span>");
        expect(maplibre.markers[0]?.offset).toEqual([-15, -40]);
    });

    test("replays draggability set before it was ever shown", () => {
        const { marker, view } = ENGINES[1]!.create({ icon: ICON });
        marker.setDraggable(true);
        marker.addTo(view);
        expect(maplibre.markers[0]?.draggable).toBe(true);
    });

    test("rewrites the live element in place rather than replacing the marker", () => {
        const { marker, view } = ENGINES[1]!.create({ icon: ICON });
        marker.addTo(view);
        const before = marker.getElement();
        marker.setIcon({ html: "<i>other</i>", className: "other", size: [10, 10], anchor: [5, 10] });
        expect(marker.getElement()).toBe(before);
        expect(maplibre.markers).toHaveLength(1);
        expect(maplibre.markers[0]?.offset).toEqual([-5, -10]);
    });

    test("follows the marker when MapLibre reports a drag finished", () => {
        const { marker, view } = ENGINES[1]!.create({ icon: ICON, draggable: true });
        marker.addTo(view);
        let ended = 0;
        marker.on("dragend", () => (ended += 1));
        const behind = maplibre.markers[0] as StubMaplibreMarker & { fire(type: string): void };
        behind.setLngLat([-0.12, 51.5]);
        behind.fire("dragend");
        expect(ended).toBe(1);
        expect(marker.getLatLng()).toEqual({ lat: 51.5, lng: -0.12 });
    });

    test("offsets the popup clear of the icon's top edge", () => {
        const { marker, view } = ENGINES[1]!.create({ icon: ICON });
        marker.addTo(view);
        marker.bindPopup("<b>hi</b>");
        marker.openPopup();
        expect(maplibre.popups[0]?.offset).toEqual([0, -40]);
        expect(maplibre.popups[0]?.lngLat).toEqual([AT.lng, AT.lat]);
    });

    test("toggles its popup on a click, the way a bound Leaflet popup does", () => {
        const { marker, view } = ENGINES[1]!.create({ icon: ICON });
        marker.addTo(view);
        marker.bindPopup("<b>hi</b>");
        ENGINES[1]!.firePointer(marker, "click");
        expect(maplibre.popups[0]?.isOpen()).toBe(true);
        ENGINES[1]!.firePointer(marker, "click");
        expect(maplibre.popups[0]?.isOpen()).toBe(false);
    });

    test("opens the popup exactly once when the page also handles the click", () => {
        // MapLibre's own `setPopup` would toggle here too, so a page calling openPopup() from its
        // click handler would see the popup close again. Nothing may install that second handler.
        const { marker, view } = ENGINES[1]!.create({ icon: ICON });
        marker.addTo(view);
        marker.bindPopup("<b>hi</b>");
        marker.on("click", () => marker.openPopup());
        ENGINES[1]!.firePointer(marker, "click");
        expect(maplibre.popups.filter((popup) => popup.isOpen())).toHaveLength(1);
    });

    test("has already opened the popup by the time the page's click listener runs", () => {
        // What select mode depends on: it closes a popup it can see the click just opened
        // (`map-page.ts`'s `_handleSelectablePinClick`). Firing listeners first would break it.
        const { marker, view } = ENGINES[1]!.create({ icon: ICON });
        marker.addTo(view);
        marker.bindPopup("<b>hi</b>");
        const sawOpen: boolean[] = [];
        marker.on("click", () => {
            sawOpen.push(maplibre.popups.some((popup) => popup.isOpen()));
            marker.closePopup();
        });
        ENGINES[1]!.firePointer(marker, "click");
        expect(sawOpen).toEqual([true]);
        expect(maplibre.popups.some((popup) => popup.isOpen())).toBe(false);
    });

    test("moves an open popup with the marker", () => {
        const { marker, view } = ENGINES[1]!.create({ icon: ICON });
        marker.addTo(view);
        marker.bindPopup("<b>hi</b>");
        marker.openPopup();
        marker.setLatLng({ lat: 51.5, lng: -0.12 });
        expect(maplibre.popups[0]?.lngLat).toEqual([-0.12, 51.5]);
    });

    test("puts its popup under the view's control, so a map-wide close reaches it", () => {
        const view = fakeMaplibreView();
        const marker = createMaplibreMarker(AT, { icon: ICON });
        marker.addTo(view as unknown as MapView);
        marker.bindPopup("<b>hi</b>");
        marker.openPopup();
        expect(view.tracked).toHaveLength(1);
        view.tracked[0]!.close();
        expect(maplibre.popups[0]?.isOpen()).toBe(false);
    });

    test("announces an opened popup so popupopen listeners fire", () => {
        const view = fakeMaplibreView();
        const marker = createMaplibreMarker(AT, { icon: ICON });
        marker.addTo(view as unknown as MapView);
        marker.bindPopup("<b>hi</b>");
        marker.openPopup();
        expect(view.opened).toHaveLength(1);
        expect(view.opened[0]!.element()).toBe(maplibre.popups[0]!.getElement());
    });

    test("stops tracking its popup once removed, so the view does not hold it forever", () => {
        const view = fakeMaplibreView();
        const marker = createMaplibreMarker(AT, { icon: ICON });
        marker.addTo(view as unknown as MapView);
        marker.bindPopup("<b>hi</b>");
        marker.openPopup();
        marker.remove();
        expect(view.tracked).toHaveLength(0);
        expect(maplibre.popups[0]?.isOpen()).toBe(false);
    });

    test("stops delivering events after it is removed", () => {
        const { marker, view } = ENGINES[1]!.create({ icon: ICON });
        marker.addTo(view);
        let clicks = 0;
        marker.on("click", () => (clicks += 1));
        const element = marker.getElement()!;
        marker.remove();
        element.dispatchEvent(pointerEvent("click"));
        expect(clicks).toBe(0);
    });

    test("keeps a popup body bound before it was ever shown", () => {
        const { marker, view } = ENGINES[1]!.create({ icon: ICON });
        marker.bindPopup("<b>early</b>");
        marker.addTo(view);
        marker.openPopup();
        expect(maplibre.popups[0]?.html).toBe("<b>early</b>");
    });
});

describe("stopPointerEvent", () => {
    test("keeps the event from reaching the map underneath", () => {
        const raw = pointerEvent("contextmenu");
        let bubbled = false;
        const host = document.createElement("div");
        const child = document.createElement("div");
        host.appendChild(child);
        host.addEventListener("contextmenu", () => (bubbled = true));
        child.addEventListener("contextmenu", (event) => stopPointerEvent({ lat: 0, lng: 0, originalEvent: event as MouseEvent }));
        child.dispatchEvent(raw);
        expect(bubbled).toBe(false);
        expect(raw.defaultPrevented).toBe(true);
    });
});
