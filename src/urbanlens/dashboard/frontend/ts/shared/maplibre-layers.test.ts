/**
 * The MapLibre layers engine has to behave like the Leaflet one through the same
 * `MapLayersInstance` contract, against a map whose style is usually still loading when the engine
 * is created. These run the real engine against a behaviourally-real MapLibre stand-in rather than
 * a real WebGL map, which bun's DOM has no canvas for.
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { createMapLayers } from "./map-layers";
import { createMaplibreMapLayers, isMaplibreMap } from "./maplibre-layers";
import type { Map as MaplibreMap } from "maplibre-gl";

interface FakeLayer {
    id: string;
    source: string;
    layout: Record<string, unknown>;
    paint: Record<string, unknown>;
}

/**
 * Enough of MapLibre's map API for the engine to run its real setup, toggling and teardown
 * against - including the constraint the engine exists to work around: `addSource`/`addLayer`
 * throw until the style has loaded.
 */
class FakeMaplibreMap {
    readonly sources = new Map<string, Record<string, unknown>>();
    readonly layers = new Map<string, FakeLayer>();
    /** Style draw order, bottom first - what `addLayer`'s `beforeId` inserts into. */
    readonly order: string[] = [];
    private styleLoaded = false;
    private readonly handlers = new Map<string, Set<(arg: unknown) => void>>();
    private readonly onceHandlers = new Map<string, Set<(arg: unknown) => void>>();
    private readonly container = document.createElement("div");

    /** Seeds a layer the page drew before the engine was created (markup shapes, pin markers). */
    constructor(preExistingLayerIds: string[] = []) {
        for (const id of preExistingLayerIds) {
            this.layers.set(id, { id, source: id, layout: {}, paint: {} });
            this.order.push(id);
        }
    }

    setLayoutProperty(id: string, name: string, value: unknown): void {
        const layer = this.layers.get(id);
        if (!layer) throw new Error(`no layer ${id}`);
        layer.layout[name] = value;
    }
    setPaintProperty(id: string, name: string, value: unknown): void {
        const layer = this.layers.get(id);
        if (!layer) throw new Error(`no layer ${id}`);
        layer.paint[name] = value;
    }
    getLayer(id: string): FakeLayer | undefined {
        return this.layers.get(id);
    }
    getSource(id: string): Record<string, unknown> | undefined {
        return this.sources.get(id);
    }
    addSource(id: string, source: Record<string, unknown>): void {
        if (!this.styleLoaded) throw new Error("Style is not done loading");
        this.sources.set(id, source);
    }
    addLayer(layer: { id: string; source: string; layout?: Record<string, unknown>; paint?: Record<string, unknown> }, beforeId?: string): void {
        if (!this.styleLoaded) throw new Error("Style is not done loading");
        this.layers.set(layer.id, { id: layer.id, source: layer.source, layout: { ...layer.layout }, paint: { ...layer.paint } });
        const at = beforeId ? this.order.indexOf(beforeId) : -1;
        if (at >= 0) this.order.splice(at, 0, layer.id);
        else this.order.push(layer.id);
    }
    getStyle(): { layers: Array<{ id: string }> } {
        return { layers: this.order.map((id) => ({ id })) };
    }
    isStyleLoaded(): boolean {
        return this.styleLoaded;
    }
    getContainer(): HTMLElement {
        return this.container;
    }
    getZoom(): number {
        return 12;
    }
    on(event: string, handler: (arg: unknown) => void): void {
        if (!this.handlers.has(event)) this.handlers.set(event, new Set());
        this.handlers.get(event)!.add(handler);
    }
    once(event: string, handler: (arg: unknown) => void): void {
        if (!this.onceHandlers.has(event)) this.onceHandlers.set(event, new Set());
        this.onceHandlers.get(event)!.add(handler);
    }
    off(event: string, handler: (arg: unknown) => void): void {
        this.handlers.get(event)?.delete(handler);
        this.onceHandlers.get(event)?.delete(handler);
    }
    listenerCount(event: string): number {
        return (this.handlers.get(event)?.size ?? 0) + (this.onceHandlers.get(event)?.size ?? 0);
    }
    fire(event: string, data: unknown = {}): void {
        for (const handler of this.handlers.get(event) ?? []) handler(data);
        const once = this.onceHandlers.get(event);
        if (once) {
            this.onceHandlers.set(event, new Set());
            for (const handler of once) handler(data);
        }
    }
    /** What MapLibre does between construction and the "load" event. */
    finishStyleLoad(): void {
        this.styleLoaded = true;
        this.fire("load");
    }
    visibilityOf(id: string): unknown {
        return this.layers.get(id)?.layout.visibility;
    }
}

function makeMap(preExisting: string[] = []): FakeMaplibreMap {
    return new FakeMaplibreMap(preExisting);
}

function asMaplibre(map: FakeMaplibreMap): MaplibreMap {
    return map as unknown as MaplibreMap;
}

const STREET = "ul-layers-street";
const DARK = "ul-layers-dark";
const TOPO = "ul-layers-topographic";
const SATELLITE = "ul-layers-satellite";
const BORDERS = "ul-layers-borders";
const RAIN = "ul-layers-weather-rain";
const CLOUDS = "ul-layers-weather-clouds";

function makeStrip(): HTMLElement {
    const root = document.createElement("div");
    root.innerHTML = `
        <button data-layers-toggle></button>
        <div data-layers-menu hidden>
            <button data-map-layer="street" data-layer-kind="base"></button>
            <button data-map-layer="terrain" data-layer-kind="base"></button>
            <button data-map-layer="satellite" data-layer-kind="base"></button>
            <button data-map-layer="borders"></button>
            <button data-map-layer="weather"></button>
            <button data-map-layer="dark"></button>
        </div>`;
    document.body.appendChild(root);
    return root;
}

function button(root: HTMLElement, key: string): HTMLElement {
    return root.querySelector<HTMLElement>(`[data-map-layer="${key}"]`)!;
}

afterEach(() => {
    document.body.innerHTML = "";
});

describe("isMaplibreMap", () => {
    test("recognises a MapLibre map by a method Leaflet does not have", () => {
        expect(isMaplibreMap(makeMap())).toBe(true);
    });

    test("rejects a Leaflet-shaped map", () => {
        const leafletish = { hasLayer: () => false, addLayer: () => {}, getContainer: () => document.createElement("div") };
        expect(isMaplibreMap(leafletish)).toBe(false);
    });
});

describe("deferred style setup", () => {
    test("adds no source or layer while the style is still loading", () => {
        const map = makeMap();
        createMaplibreMapLayers(asMaplibre(map), { contextMenu: false });

        expect(map.sources.size).toBe(0);
        expect(map.layers.size).toBe(0);
    });

    test("adds every managed layer once the style loads", () => {
        const map = makeMap();
        createMaplibreMapLayers(asMaplibre(map), { contextMenu: false });

        map.finishStyleLoad();

        expect([...map.layers.keys()].sort()).toEqual([BORDERS, DARK, SATELLITE, STREET, TOPO].sort());
    });

    test("sets up immediately when the style is already loaded", () => {
        const map = makeMap();
        map.finishStyleLoad();

        createMaplibreMapLayers(asMaplibre(map), { contextMenu: false });

        expect(map.getLayer(STREET)).toBeTruthy();
        expect(map.visibilityOf(STREET)).toBe("visible");
    });

    test("keeps its tile layers beneath whatever the page already drew", () => {
        // A markup shape or pin layer added before the engine exists must stay on top -
        // MapLibre appends by default, which would bury it under the base tiles.
        const map = makeMap(["markup-fill-0", "markup-line-0"]);
        createMaplibreMapLayers(asMaplibre(map), { contextMenu: false });

        map.finishStyleLoad();

        expect(map.order).toEqual([STREET, DARK, TOPO, SATELLITE, BORDERS, "markup-fill-0", "markup-line-0"]);
    });

    test("state chosen before the style loads is applied when it does", () => {
        const map = makeMap();
        const layers = createMaplibreMapLayers(asMaplibre(map), { contextMenu: false });

        layers.setBase("satellite");
        layers.toggleBorders();
        expect(map.layers.size).toBe(0);

        map.finishStyleLoad();

        expect(map.visibilityOf(SATELLITE)).toBe("visible");
        expect(map.visibilityOf(BORDERS)).toBe("visible");
        expect(layers.baseKey()).toBe("satellite");
    });
});

describe("base layer switching", () => {
    let map: FakeMaplibreMap;

    beforeEach(() => {
        map = makeMap();
        map.finishStyleLoad();
    });

    test("street is the default, with topo and satellite hidden", () => {
        createMaplibreMapLayers(asMaplibre(map), { contextMenu: false });

        expect(map.visibilityOf(STREET)).toBe("visible");
        expect(map.visibilityOf(TOPO)).toBe("none");
        expect(map.visibilityOf(SATELLITE)).toBe("none");
    });

    test("street stays on beneath satellite, matching the Leaflet engine's stacking", () => {
        const layers = createMaplibreMapLayers(asMaplibre(map), { contextMenu: false });

        layers.setBase("satellite");

        expect(map.visibilityOf(STREET)).toBe("visible");
        expect(map.visibilityOf(SATELLITE)).toBe("visible");
    });

    test("switching away from satellite hides it again", () => {
        const layers = createMaplibreMapLayers(asMaplibre(map), { contextMenu: false });

        layers.setBase("satellite");
        layers.setBase("topographic");

        expect(map.visibilityOf(SATELLITE)).toBe("none");
        expect(map.visibilityOf(TOPO)).toBe("visible");
    });

    test("toggleBase turns a non-street layer back off to street", () => {
        const layers = createMaplibreMapLayers(asMaplibre(map), { contextMenu: false });

        layers.toggleBase("satellite");
        expect(layers.baseKey()).toBe("satellite");
        layers.toggleBase("satellite");
        expect(layers.baseKey()).toBe("street");
    });

    test("toggleBase on street always selects street", () => {
        const layers = createMaplibreMapLayers(asMaplibre(map), { contextMenu: false });

        layers.toggleBase("street");
        expect(layers.baseKey()).toBe("street");
        layers.toggleBase("street");
        expect(layers.baseKey()).toBe("street");
    });

    test("legacy base aliases resolve the same way the Leaflet engine resolves them", () => {
        const layers = createMaplibreMapLayers(asMaplibre(map), { contextMenu: false });

        layers.setBase("topo");
        expect(layers.baseKey()).toBe("topographic");
        layers.setBase("standard");
        expect(layers.baseKey()).toBe("street");
    });

    test("defaultBase picks the starting layer", () => {
        const layers = createMaplibreMapLayers(asMaplibre(map), { defaultBase: "terrain", contextMenu: false });

        expect(layers.baseKey()).toBe("topographic");
        expect(map.visibilityOf(TOPO)).toBe("visible");
    });
});

describe("dark mode", () => {
    test("swaps street for the dark base without touching the selected overlay base", () => {
        const map = makeMap();
        map.finishStyleLoad();
        const layers = createMaplibreMapLayers(asMaplibre(map), { darkMode: "dark", contextMenu: false });

        expect(map.visibilityOf(STREET)).toBe("none");
        expect(map.visibilityOf(DARK)).toBe("visible");

        layers.setDarkMode("light");

        expect(map.visibilityOf(STREET)).toBe("visible");
        expect(map.visibilityOf(DARK)).toBe("none");
    });

    /**
     * The Leaflet engine inverts topo tiles with a CSS `invert(100%) hue-rotate(180deg)
     * brightness(90%)` filter on their own pane. MapLibre has no invert, so the engine expresses
     * the same transform in raster paint properties - see `TOPO_DARK_PAINT`'s own derivation.
     */
    test("applies the exact paint equivalent of the Leaflet topo pane's invert filter", () => {
        const map = makeMap();
        map.finishStyleLoad();
        const layers = createMaplibreMapLayers(asMaplibre(map), { darkMode: "dark", defaultBase: "topographic", contextMenu: false });

        expect(map.getLayer(TOPO)!.paint).toMatchObject({
            "raster-hue-rotate": 180,
            "raster-brightness-min": 0.9,
            "raster-brightness-max": 0,
        });

        layers.setDarkMode("light");

        expect(map.getLayer(TOPO)!.paint).toMatchObject({
            "raster-hue-rotate": 0,
            "raster-brightness-min": 0,
            "raster-brightness-max": 1,
        });
    });

    test("leaves topo uninverted in dark mode when topo is not the selected base", () => {
        const map = makeMap();
        map.finishStyleLoad();
        createMaplibreMapLayers(asMaplibre(map), { darkMode: "dark", defaultBase: "satellite", contextMenu: false });

        expect(map.getLayer(TOPO)!.paint["raster-brightness-max"]).toBe(1);
    });

    test("publishes the effective style on the container for SCSS", () => {
        const map = makeMap();
        map.finishStyleLoad();
        const layers = createMaplibreMapLayers(asMaplibre(map), { darkMode: "dark", contextMenu: false });

        expect(map.getContainer().dataset.mapStyle).toBe("dark");
        layers.toggleDark();
        expect(map.getContainer().dataset.mapStyle).toBe("light");
    });
});

describe("overlays", () => {
    test("borders toggles its layer's visibility", () => {
        const map = makeMap();
        map.finishStyleLoad();
        const layers = createMaplibreMapLayers(asMaplibre(map), { contextMenu: false });

        expect(map.visibilityOf(BORDERS)).toBe("none");
        layers.toggleBorders();
        expect(map.visibilityOf(BORDERS)).toBe("visible");
        layers.setOverlay("borders", false);
        expect(map.visibilityOf(BORDERS)).toBe("none");
    });

    test("initialOverlays turns borders on from the start", () => {
        const map = makeMap();
        map.finishStyleLoad();
        createMaplibreMapLayers(asMaplibre(map), { initialOverlays: ["borders"], contextMenu: false });

        expect(map.visibilityOf(BORDERS)).toBe("visible");
    });

    test("the borders overlay keeps the 0.6 opacity the Leaflet engine gives it", () => {
        const map = makeMap();
        map.finishStyleLoad();
        createMaplibreMapLayers(asMaplibre(map), { contextMenu: false });

        expect(map.getLayer(BORDERS)!.paint["raster-opacity"]).toBe(0.6);
    });

    test("no weather layers exist at all without an API key", () => {
        const map = makeMap();
        map.finishStyleLoad();
        const layers = createMaplibreMapLayers(asMaplibre(map), { contextMenu: false });

        layers.toggleWeather();

        expect(map.getLayer(RAIN)).toBeUndefined();
        expect(map.getLayer(CLOUDS)).toBeUndefined();
        expect(layers.getState().weather).toBe(false);
    });

    test("does not claim weather is on when initialOverlays asks for it but no key exists", () => {
        // The Leaflet engine derives this from the layers it actually added, so it cannot
        // disagree with the map; this engine tracks its own state and has to clamp it.
        const map = makeMap();
        map.finishStyleLoad();
        const root = makeStrip();
        const layers = createMaplibreMapLayers(asMaplibre(map), { root, initialOverlays: ["weather"], contextMenu: false });

        expect(layers.getState().weather).toBe(false);
        expect(button(root, "weather").classList.contains("active")).toBe(false);
    });

    test("an API key adds both weather layers and toggles them together", () => {
        const map = makeMap();
        map.finishStyleLoad();
        const layers = createMaplibreMapLayers(asMaplibre(map), { apiKey: "test-key", contextMenu: false });

        expect(map.visibilityOf(RAIN)).toBe("none");
        layers.toggleWeather();
        expect(map.visibilityOf(RAIN)).toBe("visible");
        expect(map.visibilityOf(CLOUDS)).toBe("visible");
        expect(map.sources.get(RAIN)!.tiles).toEqual([
            "https://tile.openweathermap.org/map/precipitation_new/{z}/{x}/{y}.png?appid=test-key",
        ]);
    });
});

describe("tile sources", () => {
    test("expands Leaflet's {s} subdomain token, which MapLibre has no equivalent for", () => {
        const map = makeMap();
        map.finishStyleLoad();
        createMaplibreMapLayers(asMaplibre(map), { contextMenu: false });

        const tiles = map.sources.get(STREET)!.tiles as string[];
        expect(tiles).toHaveLength(3);
        for (const url of tiles) expect(url).not.toContain("{s}");
    });

    test("carries each vendor's native depth over as the source maxzoom", () => {
        const map = makeMap();
        map.finishStyleLoad();
        createMaplibreMapLayers(asMaplibre(map), { contextMenu: false });

        // OpenTopoMap only renders to 17; MapLibre upscales past a source maxzoom the way Leaflet's maxNativeZoom does.
        expect(map.sources.get(TOPO)!.maxzoom).toBe(17);
        expect(map.sources.get(SATELLITE)!.maxzoom).toBe(19);
    });
});

describe("attribution", () => {
    function attributionFor(options: Parameters<typeof createMaplibreMapLayers>[1]): string[] {
        const map = makeMap();
        map.finishStyleLoad();
        const seen: string[] = [];
        createMaplibreMapLayers(asMaplibre(map), { ...options, contextMenu: false, onAttribution: (text) => seen.push(text) });
        return seen;
    }

    test("names the engine actually drawing the map, not Leaflet", () => {
        expect(attributionFor({}).at(-1)).toContain("MapLibre");
        expect(attributionFor({}).at(-1)).not.toContain("Leaflet");
    });

    test("credits the vendor behind the selected base layer", () => {
        expect(attributionFor({}).at(-1)).toContain("OSM · CARTO");
        expect(attributionFor({ defaultBase: "satellite" }).at(-1)).toContain("Esri");
        expect(attributionFor({ defaultBase: "topographic" }).at(-1)).toContain("OpenTopoMap");
    });

    test("updates when the base layer changes", () => {
        const map = makeMap();
        map.finishStyleLoad();
        const seen: string[] = [];
        const layers = createMaplibreMapLayers(asMaplibre(map), { contextMenu: false, onAttribution: (text) => seen.push(text) });

        layers.setBase("satellite");

        expect(seen.at(-1)).toContain("Esri");
    });

    test("does not credit Esri twice when borders sit under a satellite base", () => {
        const text = attributionFor({ defaultBase: "satellite", initialOverlays: ["borders"] }).at(-1)!;
        expect(text.match(/Esri/g)).toHaveLength(1);
    });
});

describe("the layers strip", () => {
    test("highlights the active base, overlay and dark buttons", () => {
        const map = makeMap();
        map.finishStyleLoad();
        const root = makeStrip();
        const layers = createMaplibreMapLayers(asMaplibre(map), { root, apiKey: "k", contextMenu: false });

        expect(button(root, "street").classList.contains("active")).toBe(true);

        layers.setBase("topographic");
        expect(button(root, "terrain").classList.contains("active")).toBe(true);
        expect(button(root, "street").classList.contains("active")).toBe(false);

        layers.toggleBorders();
        expect(button(root, "borders").classList.contains("active")).toBe(true);

        layers.toggleDark();
        expect(button(root, "dark").classList.contains("active")).toBe(true);
    });

    test("a button click drives the map through the engine", () => {
        const map = makeMap();
        map.finishStyleLoad();
        const root = makeStrip();
        createMaplibreMapLayers(asMaplibre(map), { root, contextMenu: false });

        button(root, "satellite").dispatchEvent(new MouseEvent("click", { bubbles: true }));

        expect(map.visibilityOf(SATELLITE)).toBe("visible");
        expect(button(root, "satellite").classList.contains("active")).toBe(true);
    });

    test("hides the weather button when the deployment has no API key", () => {
        const map = makeMap();
        map.finishStyleLoad();
        const root = makeStrip();
        createMaplibreMapLayers(asMaplibre(map), { root, contextMenu: false });

        expect((button(root, "weather") as HTMLButtonElement).hidden).toBe(true);
    });

    test("a custom toggle reports through the same button-sync path", () => {
        const map = makeMap();
        map.finishStyleLoad();
        const root = makeStrip();
        const menu = root.querySelector("[data-layers-menu]")!;
        const pins = document.createElement("button");
        pins.dataset.mapLayer = "pins";
        menu.appendChild(pins);

        let hidden = false;
        const layers = createMaplibreMapLayers(asMaplibre(map), {
            root,
            contextMenu: false,
            custom: { pins: { isActive: () => !hidden, toggle: () => (hidden = !hidden), activeWhenOff: true } },
        });

        expect(pins.classList.contains("active")).toBe(false);
        layers.toggleCustom("pins");
        // activeWhenOff: the button highlights when the feature is off.
        expect(pins.classList.contains("active")).toBe(true);
    });
});

describe("createMaplibreMapLayers destroy()", () => {
    test("releases every listener it put on the map", () => {
        const map = makeMap();
        const layers = createMaplibreMapLayers(asMaplibre(map), { loadingTarget: document.createElement("div") });

        expect(map.listenerCount("load")).toBe(1);
        expect(map.listenerCount("contextmenu")).toBe(1);
        expect(map.listenerCount("dataloading")).toBe(1);
        expect(map.listenerCount("idle")).toBe(1);

        layers.destroy();

        for (const event of ["load", "contextmenu", "dataloading", "idle"]) expect(map.listenerCount(event)).toBe(0);
    });

    test("does not bind a context menu when contextMenu is false", () => {
        const map = makeMap();
        createMaplibreMapLayers(asMaplibre(map), { contextMenu: false });

        expect(map.listenerCount("contextmenu")).toBe(0);
    });

    test("a style that loads after destroy() never touches the map", () => {
        // The dialog/thumbnail maps are torn down on an HTMX swap, which can land
        // before a slow style finishes loading.
        const map = makeMap();
        const layers = createMaplibreMapLayers(asMaplibre(map), { contextMenu: false });

        layers.destroy();
        map.finishStyleLoad();

        expect(map.layers.size).toBe(0);
    });

    test("removes the document click listener that closes the panel", () => {
        const map = makeMap();
        map.finishStyleLoad();
        const root = makeStrip();
        const layers = createMaplibreMapLayers(asMaplibre(map), { root, contextMenu: false });

        layers.openPanel();
        document.body.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect(layers.isPanelOpen()).toBe(false);

        layers.destroy();

        layers.openPanel();
        document.body.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect(layers.isPanelOpen()).toBe(true);
    });
});

describe("createMapLayers engine dispatch", () => {
    test("hands a MapLibre map to the MapLibre engine", () => {
        const map = makeMap();
        map.finishStyleLoad();

        const layers = createMapLayers(asMaplibre(map), { contextMenu: false });

        expect(map.getLayer(STREET)).toBeTruthy();
        expect(layers.baseKey()).toBe("street");
    });
});
