/**
 * Photo marker sizing and the stacked-cluster badge. Leaflet is not loaded
 * here - these are the pure bits a second map surface (album, pin detail)
 * must stay in lockstep with.
 */
import { afterEach, describe, expect, test } from "bun:test";

import { detailPinClusterRadius } from "./map-clusters";
import { createPhotoClusterGroup, createPhotoMarkerLayer, PHOTO_MARKER_BASE_SIZE, PHOTO_MARKER_MIN_SIZE, photoClusterMarkup, photoClusterRadius, photoMarkerSize, tagPhotoMarker } from "./photo-map";

describe("photoMarkerSize", () => {
    test("is full size at zoom 16 and above", () => {
        expect(photoMarkerSize(16)).toBe(PHOTO_MARKER_BASE_SIZE);
        expect(photoMarkerSize(20)).toBe(PHOTO_MARKER_BASE_SIZE);
    });

    test("never shrinks below the readable floor", () => {
        expect(photoMarkerSize(0)).toBe(PHOTO_MARKER_MIN_SIZE);
        expect(photoMarkerSize(8)).toBe(PHOTO_MARKER_MIN_SIZE);
    });
});

describe("photoClusterRadius", () => {
    test("tracks thumbnail size so overlapping squares become a stack", () => {
        expect(photoClusterRadius(16)).toBe(Math.round(PHOTO_MARKER_BASE_SIZE * 0.9));
        expect(photoClusterRadius(8)).toBe(PHOTO_MARKER_MIN_SIZE);
    });

    test("stays wide at building-level zoom so same-spot GPS photos remain stacked", () => {
        expect(photoClusterRadius(18)).toBeGreaterThan(detailPinClusterRadius(18));
        expect(photoClusterRadius(18)).toBeGreaterThan(20);
    });
});

describe("photoClusterMarkup", () => {
    test("stacks the top image over the second and shows the count", () => {
        const html = photoClusterMarkup("https://example.test/a.jpg", "https://example.test/b.jpg", 5, 44);
        expect(html).toContain("photo-cluster__img--front");
        expect(html).toContain("photo-cluster__img--back");
        expect(html).toContain('src="https://example.test/a.jpg"');
        expect(html).toContain('src="https://example.test/b.jpg"');
        expect(html).toContain('aria-label="5 photos"');
        expect(html).toContain(">5<");
        expect(html).not.toContain("marker-cluster");
    });

    test("escapes quotes in photo URLs so they cannot break the attribute", () => {
        const html = photoClusterMarkup('https://example.test/a".jpg', "https://example.test/b.jpg", 2, 44);
        expect(html).toContain("a&quot;.jpg");
        expect(html).not.toContain('src="https://example.test/a"');
    });

    test("falls back to the front image when no second photo is given", () => {
        const html = photoClusterMarkup("https://example.test/a.jpg", "", 2, 44);
        expect((html.match(/https:\/\/example\.test\/a\.jpg/g) || []).length).toBe(2);
    });
});

describe("createPhotoMarkerLayer", () => {
    /** Leaflet renders a string tooltip as HTML and an element as-is; this stands in for that and nothing else. */
    function renderTooltip(content: unknown): HTMLElement {
        const host = document.createElement("div");
        if (typeof content === "string") host.innerHTML = content;
        else if (content instanceof Node) host.appendChild(content);
        return host;
    }

    test("a photo caption shows as text in its tooltip, never as markup", () => {
        const tooltips: unknown[] = [];
        const chain = (): Record<string, unknown> => {
            const self: Record<string, unknown> = {};
            for (const name of ["addTo", "on", "addLayer", "removeLayer", "setIcon"]) self[name] = () => self;
            self.bindTooltip = (content: unknown) => {
                tooltips.push(content);
                return self;
            };
            return self;
        };
        const globals = globalThis as Record<string, unknown>;
        const previous = globals.L;
        globals.L = { marker: chain, layerGroup: chain, divIcon: (options: unknown) => options };
        try {
            const map = { getZoom: () => 16, getMaxZoom: () => Infinity, on: () => map } as unknown as L.Map;
            createPhotoMarkerLayer(map).set({ id: 1, url: "/t.jpg", lat: 1, lng: 2, caption: '<img id="caption-canary" src=x>' });
        } finally {
            globals.L = previous;
        }

        expect(tooltips).toHaveLength(1);
        const rendered = renderTooltip(tooltips[0]);
        expect(rendered.querySelector("#caption-canary")).toBeNull();
        expect(rendered.textContent).toBe('<img id="caption-canary" src=x>');
    });
});

/**
 * Just enough of Leaflet for the layer's own bookkeeping: each marker owns one icon element, as
 * Leaflet's does, and `setIcon` is what replaces that element.
 */
interface FakeMarker {
    options: { icon?: { className?: string } };
    element: HTMLElement;
    setIconCalls: number;
    zIndexOffset: number;
    handlers: Record<string, Array<() => void>>;
    latlng: { lat: number; lng: number };
    fire(event: string): void;
    getLatLng(): { lat: number; lng: number };
}

function installFakeLeaflet(): { markers: FakeMarker[]; removed: FakeMarker[]; clusterOptions: Array<Record<string, unknown>>; refreshed: FakeMarker[] } {
    const markers: FakeMarker[] = [];
    const removed: FakeMarker[] = [];
    const refreshed: FakeMarker[] = [];
    const clusterOptions: Array<Record<string, unknown>> = [];
    const group = (): Record<string, unknown> => {
        const self: Record<string, unknown> = {};
        self.addTo = () => self;
        self.addLayer = () => self;
        self.removeLayer = (marker: FakeMarker) => {
            removed.push(marker);
            return self;
        };
        return self;
    };
    const marker = (latlng: [number, number], options: { icon?: { className?: string } }): FakeMarker & Record<string, unknown> => {
        const element = document.createElement("div");
        element.className = options.icon?.className ?? "";
        const self = {
            options: { ...options },
            element,
            setIconCalls: 0,
            zIndexOffset: 0,
            handlers: {} as Record<string, Array<() => void>>,
            fire(event: string) {
                for (const fn of self.handlers[event] ?? []) fn();
            },
            on(event: string, fn: () => void) {
                (self.handlers[event] ??= []).push(fn);
                return self;
            },
            addTo: () => self,
            bindTooltip: () => self,
            latlng: { lat: 0, lng: 0 },
            getElement: () => self.element,
            getLatLng: () => self.latlng,
            setLatLng(latlng: [number, number]) {
                self.latlng = { lat: latlng[0], lng: latlng[1] };
                return self;
            },
            setIcon(icon: { className?: string }) {
                self.setIconCalls += 1;
                self.options.icon = icon;
                self.element = document.createElement("div");
                self.element.className = icon.className ?? "";
                return self;
            },
            setZIndexOffset(offset: number) {
                self.zIndexOffset = offset;
                return self;
            },
        };
        self.latlng = { lat: latlng[0], lng: latlng[1] };
        markers.push(self);
        return self;
    };
    (globalThis as Record<string, unknown>).L = {
        marker,
        layerGroup: group,
        divIcon: (options: unknown) => options,
        markerClusterGroup: (options: Record<string, unknown>) => {
            clusterOptions.push(options);
            const self = group();
            self.getVisibleParent = (m: FakeMarker) => m;
            self.refreshClusters = (m: FakeMarker) => {
                refreshed.push(m);
                return self;
            };
            return self;
        },
    };
    return { markers, removed, clusterOptions, refreshed };
}

const clusteringMap = { getZoom: () => 18, getMaxZoom: () => 22, on: () => clusteringMap } as unknown as L.Map;

describe("photo marker highlighting", () => {
    const previousL = (globalThis as Record<string, unknown>).L;
    afterEach(() => {
        (globalThis as Record<string, unknown>).L = previousL;
    });

    test("restyles the marker's own element, so a pointer resting on it is not left on a replaced one", () => {
        const fake = installFakeLeaflet();
        const layer = createPhotoMarkerLayer(clusteringMap, { onMove: () => Promise.resolve() });
        layer.set({ id: 1, url: "/a.jpg", lat: 1, lng: 2, movable: true });
        const marker = fake.markers[0]!;
        const element = marker.element;

        layer.highlight(1, true);
        expect(marker.setIconCalls).toBe(0);
        expect(marker.element).toBe(element);
        expect(element.classList.contains("is-highlighted")).toBe(true);

        layer.highlight(1, false);
        expect(marker.element).toBe(element);
        expect(element.classList.contains("is-highlighted")).toBe(false);
    });

    test("a highlighted marker draws above its neighbours, and an icon rebuilt later keeps the highlight", () => {
        const fake = installFakeLeaflet();
        const layer = createPhotoMarkerLayer(clusteringMap);
        layer.set({ id: 1, url: "/a.jpg", lat: 1, lng: 2 });
        const marker = fake.markers[0]!;

        layer.highlight(1, true);
        expect(marker.zIndexOffset).toBeGreaterThan(0);
        expect(marker.options.icon?.className).toContain("is-highlighted");

        layer.highlight(1, false);
        expect(marker.zIndexOffset).toBe(0);
        expect(marker.options.icon?.className).not.toContain("is-highlighted");
    });

    test("hovering a marker reports the hover once, without rebuilding the marker under the pointer", () => {
        const fake = installFakeLeaflet();
        const hovers: Array<[number, boolean]> = [];
        const layer = createPhotoMarkerLayer(clusteringMap, {
            onHover: (id, on) => {
                hovers.push([id, on]);
                layer.highlight(id, on);
            },
        });
        layer.set({ id: 7, url: "/a.jpg", lat: 1, lng: 2 });
        const marker = fake.markers[0]!;

        marker.fire("mouseover");
        expect(hovers).toEqual([[7, true]]);
        expect(marker.setIconCalls).toBe(0);
    });

    test("flash marks the photo for a moment and then lets it go", async () => {
        const fake = installFakeLeaflet();
        const layer = createPhotoMarkerLayer(clusteringMap);
        layer.set({ id: 1, url: "/a.jpg", lat: 1, lng: 2 });
        const element = fake.markers[0]!.element;

        expect(layer.flash(1, 20)).toBe(true);
        expect(element.classList.contains("is-flashing")).toBe(true);
        expect(element.classList.contains("is-highlighted")).toBe(true);
        await new Promise((resolve) => setTimeout(resolve, 60));
        expect(element.classList.contains("is-flashing")).toBe(false);
        expect(element.classList.contains("is-highlighted")).toBe(false);
        expect(layer.flash(99, 20)).toBe(false);
    });

    test("a flash ending does not clear a highlight the pointer still holds", async () => {
        const fake = installFakeLeaflet();
        const layer = createPhotoMarkerLayer(clusteringMap);
        layer.set({ id: 1, url: "/a.jpg", lat: 1, lng: 2 });
        const element = fake.markers[0]!.element;

        layer.flash(1, 20);
        layer.highlight(1, true);
        await new Promise((resolve) => setTimeout(resolve, 60));
        expect(element.classList.contains("is-highlighted")).toBe(true);
    });

    test("highlighting a photo inside a cluster redraws that cluster", () => {
        const fake = installFakeLeaflet();
        const layer = createPhotoMarkerLayer(clusteringMap);
        layer.set({ id: 1, url: "/a.jpg", lat: 1, lng: 2 });
        const marker = fake.markers[0]!;
        (layer.layer as unknown as { getVisibleParent: () => unknown }).getVisibleParent = () => ({ cluster: true });

        layer.highlight(1, true);
        expect(fake.refreshed.includes(marker), "the cluster holding the photo was not redrawn").toBe(true);
    });
});

describe("photo marker dragging", () => {
    const previousL = (globalThis as Record<string, unknown>).L;
    afterEach(() => {
        (globalThis as Record<string, unknown>).L = previousL;
    });

    test("starting a drag leaves the marker in its layer: Leaflet ends the drag of a marker that leaves the map", () => {
        const fake = installFakeLeaflet();
        const layer = createPhotoMarkerLayer(clusteringMap, { onMove: () => Promise.resolve() });
        layer.set({ id: 1, url: "/a.jpg", lat: 1, lng: 2, movable: true });
        const marker = fake.markers[0]!;

        marker.fire("dragstart");
        marker.fire("drag");
        expect(fake.removed.includes(marker), "the marker was pulled off its layer mid-drag").toBe(false);
    });
});

describe("refreshing the photo layer", () => {
    const previousL = (globalThis as Record<string, unknown>).L;
    afterEach(() => {
        (globalThis as Record<string, unknown>).L = previousL;
    });

    test("leaves a photo that is being dragged alone, so the drag is not ended under the pointer", () => {
        const fake = installFakeLeaflet();
        const layer = createPhotoMarkerLayer(clusteringMap, { onMove: () => Promise.resolve() });
        const photo = { id: 1, url: "/a.jpg", lat: 1, lng: 2, movable: true };
        layer.set(photo);
        const dragged = fake.markers[0]!;
        dragged.fire("dragstart");

        layer.replaceAll([{ ...photo, lat: 5, lng: 6 }, { id: 2, url: "/b.jpg", lat: 1, lng: 2 }]);
        expect(fake.removed.includes(dragged), "the dragged marker was taken off the map").toBe(false);
        expect(dragged.getLatLng()).toEqual({ lat: 1, lng: 2 });
        expect(fake.markers.length).toBe(2);
    });

    test("moves an unchanged photo's own marker rather than rebuilding it, keeping its highlight", () => {
        const fake = installFakeLeaflet();
        const layer = createPhotoMarkerLayer(clusteringMap);
        layer.set({ id: 1, url: "/a.jpg", lat: 1, lng: 2 });
        const marker = fake.markers[0]!;
        layer.highlight(1, true);

        layer.replaceAll([{ id: 1, url: "/a.jpg", lat: 3, lng: 4 }]);
        expect(fake.markers.length).toBe(1);
        expect(fake.removed.includes(marker)).toBe(false);
        expect(marker.getLatLng()).toEqual({ lat: 3, lng: 4 });
        expect(marker.element.classList.contains("is-highlighted")).toBe(true);
    });

    test("rebuilds a photo whose picture or permissions changed", () => {
        const fake = installFakeLeaflet();
        const layer = createPhotoMarkerLayer(clusteringMap, { onMove: () => Promise.resolve() });
        layer.set({ id: 1, url: "/a.jpg", lat: 1, lng: 2, movable: true });
        layer.set({ id: 1, url: "/a2.jpg", lat: 1, lng: 2, movable: true });
        layer.set({ id: 1, url: "/a2.jpg", lat: 1, lng: 2, movable: false });
        expect(fake.markers.length).toBe(3);
        expect(fake.removed.length).toBe(2);
    });
});

describe("photo cluster badge", () => {
    const previousL = (globalThis as Record<string, unknown>).L;
    afterEach(() => {
        (globalThis as Record<string, unknown>).L = previousL;
    });

    type IconFactory = (cluster: { getAllChildMarkers(): L.Marker[] }) => { html: string; className: string };

    function clusterOf(...entries: Array<{ id: number; url: string }>): { markers: L.Marker[]; cluster: { getAllChildMarkers(): L.Marker[] } } {
        const markers = entries.map(({ id, url }) => {
            const m = {} as L.Marker;
            tagPhotoMarker(m, url, id);
            return m;
        });
        return { markers, cluster: { getAllChildMarkers: () => markers } };
    }

    test("shows the newest photo on top", () => {
        const fake = installFakeLeaflet();
        createPhotoClusterGroup(clusteringMap);
        const iconFor = fake.clusterOptions[0]!.iconCreateFunction as IconFactory;
        const { cluster } = clusterOf({ id: 3, url: "/old.jpg" }, { id: 9, url: "/new.jpg" });

        const icon = iconFor(cluster);
        expect(icon.html.indexOf("/new.jpg")).toBeGreaterThan(icon.html.indexOf("/old.jpg"));
        expect(icon.html).toMatch(/photo-cluster__img--front" src="\/new\.jpg"/);
        expect(icon.className).not.toContain("is-highlighted");
    });

    test("puts a highlighted photo on top while it is highlighted", () => {
        const fake = installFakeLeaflet();
        const layer = createPhotoMarkerLayer(clusteringMap);
        layer.set({ id: 3, url: "/old.jpg", lat: 1, lng: 2 });
        layer.set({ id: 9, url: "/new.jpg", lat: 1, lng: 2 });
        const iconFor = fake.clusterOptions[0]!.iconCreateFunction as IconFactory;
        const cluster = { getAllChildMarkers: () => fake.markers as unknown as L.Marker[] };

        layer.highlight(3, true);
        const lit = iconFor(cluster);
        expect(lit.html).toMatch(/photo-cluster__img--front" src="\/old\.jpg"/);
        expect(lit.className).toContain("is-highlighted");

        layer.highlight(3, false);
        expect(iconFor(cluster).html).toMatch(/photo-cluster__img--front" src="\/new\.jpg"/);
    });

    test("a flashing photo's cluster flashes too", () => {
        const fake = installFakeLeaflet();
        const layer = createPhotoMarkerLayer(clusteringMap);
        layer.set({ id: 3, url: "/old.jpg", lat: 1, lng: 2 });
        layer.set({ id: 9, url: "/new.jpg", lat: 1, lng: 2 });
        const iconFor = fake.clusterOptions[0]!.iconCreateFunction as IconFactory;

        layer.flash(3, 1000);
        expect(iconFor({ getAllChildMarkers: () => fake.markers as unknown as L.Marker[] }).className).toContain("is-flashing");
    });
});
