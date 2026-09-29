/**
 * Vault > Photos' "Create a pin" dialog, driven against a stand-in Leaflet.
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import type { LocationSearchAttachOptions } from "./location-search-engine";
import { bboxParam, nearbyPinPopup, PhotoPinConfirm } from "./photo-pin-confirm";

type Handler = (e: { latlng: { lat: number; lng: number } }) => void;

class FakeMap {
    handlers: Record<string, Handler[]> = {};
    view: [{ lat: number; lng: number } | number[], number] | null = null;
    removed = false;
    setView(latlng: { lat: number; lng: number } | number[], zoom: number): this {
        this.view = [latlng, zoom];
        return this;
    }
    on(event: string, fn: Handler): this {
        (this.handlers[event] ??= []).push(fn);
        return this;
    }
    fire(event: string, latlng = { lat: 0, lng: 0 }): void {
        this.handlers[event]?.forEach((fn) => fn({ latlng }));
    }
    remove(): void {
        this.removed = true;
    }
    invalidateSize(): void {}
    getBounds() {
        return { getSouth: () => 1, getWest: () => 2, getNorth: () => 3, getEast: () => 4 };
    }
}

class FakeMarker {
    handlers: Record<string, Handler[]> = {};
    constructor(public latlng: { lat: number; lng: number }) {}
    addTo(): this {
        return this;
    }
    on(event: string, fn: Handler): this {
        (this.handlers[event] ??= []).push(fn);
        return this;
    }
    getLatLng() {
        return this.latlng;
    }
    setLatLng(latlng: { lat: number; lng: number }): this {
        this.latlng = latlng;
        return this;
    }
}

class FakeCircle {
    popup: HTMLElement | null = null;
    constructor(public latlng: number[]) {}
    bindPopup(content: HTMLElement): this {
        this.popup = content;
        return this;
    }
    addTo(layer: FakeLayer): this {
        layer.layers.push(this);
        return this;
    }
}

class FakeLayer {
    layers: FakeCircle[] = [];
    addTo(): this {
        return this;
    }
    clearLayers(): void {
        this.layers = [];
    }
}

let maps: FakeMap[];
let markers: FakeMarker[];
let layers: FakeLayer[];
let ajax: Array<{ verb: string; url: string; options: Record<string, unknown> }>;
let search: LocationSearchAttachOptions | null;
let pins: unknown[];
let fetched: string[];
const realFetch = globalThis.fetch;

const BODY = `
<div id="photo-pin-confirm" data-image-id="7" data-lat="40.5" data-lng="-73.25" data-pins-url="/map/pins/"
     data-log-visit-url="/vault/photos/7/log-visit/" data-local-url="/l/" data-nominatim-url="/n/" data-places-url="/p/" data-resolve-url="/r/">
  <div id="photo-pin-confirm-map"></div>
  <input id="photo-pin-confirm-lat"><input id="photo-pin-confirm-lng"><input id="photo-pin-confirm-name">
</div>`;

beforeEach(() => {
    maps = [];
    markers = [];
    layers = [];
    ajax = [];
    search = null;
    pins = [];
    fetched = [];
    (globalThis as Record<string, unknown>).L = {
        map: () => {
            const map = new FakeMap();
            maps.push(map);
            return map;
        },
        marker: (latlng: number[]) => {
            const marker = new FakeMarker({ lat: latlng[0]!, lng: latlng[1]! });
            markers.push(marker);
            return marker;
        },
        layerGroup: () => {
            const layer = new FakeLayer();
            layers.push(layer);
            return layer;
        },
        circleMarker: (latlng: number[]) => new FakeCircle(latlng),
        latLng: (lat: number, lng: number) => ({ lat, lng }),
    };
    window.MapLayers = { create: () => ({}) } as unknown as typeof window.MapLayers;
    window.LocationSearchEngine = {
        attach: (_prefix: string, options: LocationSearchAttachOptions) => {
            search = options;
            return null;
        },
    } as unknown as typeof window.LocationSearchEngine;
    window.htmx = {
        process: () => undefined,
        trigger: () => undefined,
        ajax: async (verb, url, options) => {
            ajax.push({ verb, url, options });
            if (verb === "GET") document.getElementById("photo-pin-confirm-body")!.innerHTML = BODY;
        },
    };
    globalThis.fetch = (async (input: RequestInfo | URL) => {
        fetched.push(String(input));
        return new Response(JSON.stringify({ pins }));
    }) as typeof fetch;
    document.body.innerHTML = '<dialog id="photo-pin-confirm-dialog"><div id="photo-pin-confirm-body"></div></dialog>';
});

afterEach(() => {
    globalThis.fetch = realFetch;
    document.body.innerHTML = "";
});

async function settle(): Promise<void> {
    await new Promise((resolve) => setTimeout(resolve, 0));
}

function dialog(): HTMLDialogElement {
    return document.getElementById("photo-pin-confirm-dialog") as HTMLDialogElement;
}

function value(id: string): string {
    return (document.getElementById(id) as HTMLInputElement).value;
}

describe("opening the dialog", () => {
    test("loads the photo's body, opens, and centres a draggable marker on the photo", async () => {
        await new PhotoPinConfirm().load("/vault/photos/7/confirm-pin/");
        expect(ajax[0]).toMatchObject({ verb: "GET", url: "/vault/photos/7/confirm-pin/" });
        expect(dialog().open).toBe(true);
        expect(maps[0]!.view).toEqual([[40.5, -73.25], 15]);
        expect(markers[0]!.latlng).toEqual({ lat: 40.5, lng: -73.25 });
    });

    test("reopening for another photo tears down the previous map", async () => {
        const confirm = new PhotoPinConfirm();
        await confirm.load("/a/");
        await confirm.load("/b/");
        expect(maps).toHaveLength(2);
        expect(maps[0]!.removed).toBe(true);
        expect(maps[1]!.removed).toBe(false);
    });
});

describe("placing the pin", () => {
    test("dragging the marker or clicking the map writes the placement the form posts", async () => {
        await new PhotoPinConfirm().load("/a/");
        markers[0]!.setLatLng({ lat: 1.23456789, lng: 2.5 });
        markers[0]!.handlers.dragend?.forEach((fn) => fn({ latlng: { lat: 0, lng: 0 } }));
        expect([value("photo-pin-confirm-lat"), value("photo-pin-confirm-lng")]).toEqual(["1.234568", "2.500000"]);

        maps[0]!.fire("click", { lat: -5, lng: 6 });
        expect(markers[0]!.latlng).toEqual({ lat: -5, lng: 6 });
        expect(value("photo-pin-confirm-lat")).toBe("-5.000000");
    });

    test("a searched place moves the marker and names an unnamed pin, without overwriting a typed name", async () => {
        await new PhotoPinConfirm().load("/a/");
        search!.onSelect({ lat: 10, lng: 20, zoom: 0, title: "Old Mill", type: "place" });
        expect(markers[0]!.latlng).toEqual({ lat: 10, lng: 20 });
        expect(maps[0]!.view).toEqual([{ lat: 10, lng: 20 }, 16]);
        expect(value("photo-pin-confirm-name")).toBe("Old Mill");

        search!.onSelect({ lat: 11, lng: 21, zoom: 12, title: "Elsewhere", type: "place" });
        expect(value("photo-pin-confirm-name")).toBe("Old Mill");
    });
});

describe("filing onto an existing pin", () => {
    test("nearby pins come from the map's bounds, skipping any without a place or slug", async () => {
        pins = [{ latitude: 1, longitude: 2, slug: "mill", name: "Mill" }, { latitude: null, longitude: 2, slug: "x" }, { latitude: 1, longitude: 2 }];
        await new PhotoPinConfirm().load("/a/");
        await settle();
        expect(fetched[0]).toBe(`/map/pins/?bbox=${encodeURIComponent("1,2,3,4")}`);
        expect(layers[0]!.layers.map((c) => c.latlng)).toEqual([[1, 2]]);
    });

    test("choosing one posts the photo onto it and closes the dialog", async () => {
        pins = [{ latitude: 1, longitude: 2, slug: "mill", name: "Mill" }];
        await new PhotoPinConfirm().load("/a/");
        await settle();
        layers[0]!.layers[0]!.popup!.querySelector("button")!.click();
        await settle();
        expect(ajax[1]).toEqual({ verb: "POST", url: "/vault/photos/7/log-visit/", options: { target: "#photo-card-7", swap: "outerHTML", values: { pin_slug: "mill" } } });
        expect(dialog().open).toBe(false);
    });

    test("one of the viewer's pins picked in search files the photo instead of moving the marker", async () => {
        await new PhotoPinConfirm().load("/a/");
        search!.onSelect({ lat: 10, lng: 20, zoom: 16, title: "Mill", type: "pin", pinSlug: "mill" });
        await settle();
        expect(markers[0]!.latlng).toEqual({ lat: 40.5, lng: -73.25 });
        expect(ajax[1]?.options.values).toEqual({ pin_slug: "mill" });
    });
});

describe("nearbyPinPopup", () => {
    test("a pin's name is text, never markup", () => {
        const popup = nearbyPinPopup('<img src=x onerror="alert(1)">', () => undefined);
        expect(popup.querySelector("img")).toBeNull();
        expect(popup.querySelector("strong")?.textContent).toBe('<img src=x onerror="alert(1)">');
    });

    test("an unnamed pin says so", () => {
        expect(nearbyPinPopup("", () => undefined).querySelector("strong")?.textContent).toBe("Unnamed pin");
    });
});

test("bboxParam orders the bounds south, west, north, east", () => {
    const bounds = { getSouth: () => 1, getWest: () => 2, getNorth: () => 3, getEast: () => 4 };
    expect(bboxParam(bounds as unknown as Parameters<typeof bboxParam>[0])).toBe("1,2,3,4");
});
