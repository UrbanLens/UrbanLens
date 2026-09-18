/**
 * normalizeBase() mirrors LEGACY_LAYER_MODE_ALIASES in dashboard/models/markup/meta.py.
 */
import { afterEach, describe, expect, test } from "bun:test";
import { createMapLayers, normalizeBase, tileLayer } from "./map-layers";

describe("normalizeBase", () => {
    test("passes canonical keys through unchanged", () => {
        expect(normalizeBase("street")).toBe("street");
        expect(normalizeBase("topographic")).toBe("topographic");
        expect(normalizeBase("satellite")).toBe("satellite");
    });

    test("maps legacy aliases to their canonical key", () => {
        expect(normalizeBase("standard")).toBe("street");
        expect(normalizeBase("osm")).toBe("street");
        expect(normalizeBase("topo")).toBe("topographic");
        expect(normalizeBase("terrain")).toBe("topographic");
    });

    test("is case-insensitive", () => {
        expect(normalizeBase("STREET")).toBe("street");
        expect(normalizeBase("Topo")).toBe("topographic");
        expect(normalizeBase("Satellite")).toBe("satellite");
    });

    test("falls back to street for unknown or missing values", () => {
        expect(normalizeBase("nonsense")).toBe("street");
        expect(normalizeBase("")).toBe("street");
        expect(normalizeBase(null)).toBe("street");
        expect(normalizeBase(undefined)).toBe("street");
    });
});

const realL = (globalThis as Record<string, unknown>).L;

function stubLeaflet(): { calls: Array<{ url: string; options: Record<string, unknown> }> } {
    const state = { calls: [] as Array<{ url: string; options: Record<string, unknown> }> };
    (globalThis as Record<string, unknown>).L = {
        tileLayer: (url: string, options: Record<string, unknown>) => {
            state.calls.push({ url, options });
            return { __kind: "tileLayer", url, options };
        },
    };
    return state;
}

afterEach(() => {
    (globalThis as Record<string, unknown>).L = realL;
});

/**
 * OSM's own tile servers enforce a usage policy (osm.wiki/Blocked) against
 * unauthorized production hotlinking - a burst of requests on zoom-out (a new
 * zoom level's worth of tiles, all uncached) trips it and OSM answers with a
 * rendered "Access blocked" warning tile, at a 200 status, so it isn't caught
 * by errorTileUrl below (that only fires on an actual load failure). Every
 * built-in base layer must instead go through a vendor whose terms permit
 * this, the way "dark" already uses CARTO instead of OSM's own servers.
 */
describe("built-in tile sources do not hotlink OSM's own policy-enforced servers", () => {
    test.each(["street", "dark", "topographic", "satellite"])("%s does not point at tile.openstreetmap.org", (kind) => {
        const state = stubLeaflet();
        tileLayer(kind);
        expect(state.calls[0]?.url).not.toContain("tile.openstreetmap.org");
    });
});

/**
 * A failed base tile (a transient 403/5xx from the vendor CDN, most often
 * seen bursting on zoom-out) must render as a neutral placeholder rather
 * than a browser broken-image icon or the vendor's own error graphic.
 */
describe("tileLayer errorTileUrl", () => {
    test.each(["street", "dark", "topographic", "satellite"])("%s base layer gets an opaque grey placeholder", (kind) => {
        const state = stubLeaflet();
        tileLayer(kind);
        expect(state.calls).toHaveLength(1);
        const errorTileUrl = state.calls[0]?.options.errorTileUrl as string;
        expect(errorTileUrl).toContain("data:image/svg+xml");
        expect(errorTileUrl).not.toBe("");
    });

    test("the borders overlay gets a transparent placeholder, not the opaque base one", () => {
        const state = stubLeaflet();
        tileLayer("borders");
        const errorTileUrl = state.calls[0]?.options.errorTileUrl as string;
        expect(errorTileUrl).toContain("data:image/gif");
    });

    test("legacy base aliases still resolve to a placeholder", () => {
        const state = stubLeaflet();
        tileLayer("topo");
        expect(state.calls[0]?.options.errorTileUrl).toBeTruthy();
    });

    test("an unknown kind falls back to street's placeholder", () => {
        const state = stubLeaflet();
        tileLayer("nonsense");
        const streetState = stubLeaflet();
        tileLayer("street");
        expect(state.calls[0]?.options.errorTileUrl).toBe(streetState.calls[0]?.options.errorTileUrl);
    });

    test("extraOptions can override the placeholder without losing the rest of the defaults", () => {
        const state = stubLeaflet();
        tileLayer("street", { errorTileUrl: "custom.png" });
        expect(state.calls[0]?.options.errorTileUrl).toBe("custom.png");
        expect(state.calls[0]?.options.maxZoom).toBe(21);
    });
});

/**
 * A minimal but behaviorally-real L.Map stand-in: enough of the layer/pane
 * API for createMapLayers to run its real setup and teardown logic against,
 * without pulling in actual Leaflet.
 */
class FakeMap {
    private readonly activeLayers = new Set<unknown>();
    private readonly panes = new Map<string, { style: Record<string, string> }>();
    private readonly handlers = new Map<string, Set<(...args: never[]) => void>>();
    private readonly container = document.createElement("div");

    getPane(name: string) {
        return this.panes.get(name);
    }
    createPane(name: string) {
        const pane = { style: {} as Record<string, string> };
        this.panes.set(name, pane);
        return pane;
    }
    hasLayer(layer: unknown): boolean {
        return this.activeLayers.has(layer);
    }
    addLayer(layer: unknown): void {
        this.activeLayers.add(layer);
    }
    removeLayer(layer: unknown): void {
        this.activeLayers.delete(layer);
    }
    on(events: string, handler: (...args: never[]) => void): void {
        for (const event of events.split(" ")) {
            if (!this.handlers.has(event)) this.handlers.set(event, new Set());
            this.handlers.get(event)!.add(handler);
        }
    }
    off(events: string, handler: (...args: never[]) => void): void {
        for (const event of events.split(" ")) this.handlers.get(event)?.delete(handler);
    }
    getContainer() {
        return this.container;
    }
    listenerCount(event: string): number {
        return this.handlers.get(event)?.size ?? 0;
    }
}

function stubLeafletForMapLayers(): void {
    (globalThis as Record<string, unknown>).L = {
        tileLayer: () => {
            const layer = { addTo: (map: FakeMap) => (map.addLayer(layer), layer) };
            return layer;
        },
    };
}

function stubMatchMedia(): { addCalls: number; removeCalls: number; fire: () => void } {
    const tracker = { addCalls: 0, removeCalls: 0, fire: () => {} };
    (globalThis as Record<string, unknown>).matchMedia = () => {
        let handler: (() => void) | null = null;
        return {
            matches: false,
            addEventListener: (_event: string, listener: () => void) => {
                tracker.addCalls++;
                handler = listener;
                tracker.fire = () => handler?.();
            },
            removeEventListener: (_event: string, listener: () => void) => {
                if (listener === handler) tracker.removeCalls++;
            },
        };
    };
    return tracker;
}

function makeToggleRoot(): HTMLElement {
    const root = document.createElement("div");
    root.innerHTML = '<button data-layers-toggle></button><div data-layers-menu hidden></div>';
    document.body.appendChild(root);
    return root;
}

describe("createMapLayers destroy()", () => {
    afterEach(() => {
        (globalThis as Record<string, unknown>).L = realL;
        delete (globalThis as Record<string, unknown>).matchMedia;
        document.body.innerHTML = "";
    });

    test("removes the document click listener that closes the panel", () => {
        stubLeafletForMapLayers();
        const map = new FakeMap();
        const root = makeToggleRoot();
        const layers = createMapLayers(map as unknown as L.Map, { root, contextMenu: false });

        layers.openPanel();
        expect(root.classList.contains("is-open")).toBe(true);
        document.body.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect(root.classList.contains("is-open")).toBe(false);

        layers.destroy();

        layers.openPanel();
        expect(root.classList.contains("is-open")).toBe(true);
        document.body.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        // Before the fix, the leaked listener from a still-earlier instance
        // would still close this - after it, nothing is listening any more.
        expect(root.classList.contains("is-open")).toBe(true);
    });

    test("removes the prefers-color-scheme listener when darkMode is 'system'", () => {
        stubLeafletForMapLayers();
        const tracker = stubMatchMedia();
        const map = new FakeMap();
        const layers = createMapLayers(map as unknown as L.Map, { darkMode: "system", contextMenu: false });

        expect(tracker.addCalls).toBe(1);
        layers.destroy();
        expect(tracker.removeCalls).toBe(1);
    });

    test("does not touch matchMedia at all outside 'system' mode", () => {
        stubLeafletForMapLayers();
        const tracker = stubMatchMedia();
        const map = new FakeMap();
        const layers = createMapLayers(map as unknown as L.Map, { darkMode: "light", contextMenu: false });

        expect(tracker.addCalls).toBe(0);
        layers.destroy();
        expect(tracker.removeCalls).toBe(0);
    });

    test("unhooks its layeradd/layerremove listener from the map", () => {
        stubLeafletForMapLayers();
        const map = new FakeMap();
        const layers = createMapLayers(map as unknown as L.Map, { contextMenu: false });

        expect(map.listenerCount("layeradd")).toBeGreaterThan(0);
        layers.destroy();
        expect(map.listenerCount("layeradd")).toBe(0);
    });

    test("a second create+destroy cycle on the same globals leaves no listeners behind", () => {
        stubLeafletForMapLayers();
        const tracker = stubMatchMedia();
        for (let i = 0; i < 3; i++) {
            const map = new FakeMap();
            const root = makeToggleRoot();
            const layers = createMapLayers(map as unknown as L.Map, { root, darkMode: "system", contextMenu: false });
            layers.destroy();
        }
        expect(tracker.addCalls).toBe(3);
        expect(tracker.removeCalls).toBe(3);
    });
});
