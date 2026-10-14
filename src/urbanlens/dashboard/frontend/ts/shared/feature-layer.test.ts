/**
 * A plugin's pin-map layer: off until toggled, fetched once, drawn beneath the page's own shapes, and explained by the
 * notice its endpoint sends.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { createFeatureLayer, FEATURE_LAYER_PANE, featureLabelHtml, type FeatureLayerConfig, type FeatureLayerNotice, type FeatureLayerResponse, featureStyle, parseFeatureLayers } from "./feature-layer";
import { HttpError } from "./fetch-json";

declare const L: typeof import("leaflet");

const realL = (globalThis as Record<string, unknown>).L;
const CONFIG: FeatureLayerConfig = { key: "zones", url: "/dashboard/pin/mill/zones/json/", attribution: "Zones: Example Agency" };

const ZONE: FeatureLayerResponse["features"][number] = {
    type: "Feature",
    geometry: {
        type: "Polygon",
        coordinates: [
            [
                [-79.1, 35.1],
                [-78.9, 35.1],
                [-78.9, 35.2],
                [-79.1, 35.1],
            ],
        ],
    },
    properties: { color: "#556b2f", label: "Training area", name: "NORTH RANGE" },
};

function answer(overrides: Partial<FeatureLayerResponse> = {}): FeatureLayerResponse {
    return { type: "FeatureCollection", features: [ZONE], notice: { kind: "info", message: "This pin is inside NORTH RANGE." }, ...overrides };
}

let map: L.Map;
let notices: FeatureLayerNotice[];
let calls: string[];

function fetcherReturning(...results: (FeatureLayerResponse | Error)[]) {
    return async <T>(url: string): Promise<T | null> => {
        calls.push(url);
        const next = results.shift();
        if (next instanceof Error) throw next;
        return (next ?? null) as T | null;
    };
}

function drawnPaths(): number {
    return map.getPane(FEATURE_LAYER_PANE)?.querySelectorAll("path").length ?? 0;
}

beforeAll(async () => {
    (globalThis as Record<string, unknown>).L = (await import("leaflet")).default;
});

afterAll(() => {
    (globalThis as Record<string, unknown>).L = realL;
});

beforeEach(() => {
    document.body.innerHTML = '<div id="map" style="width: 400px; height: 300px"></div><button data-map-layer="zones"></button>';
    map = L.map("map", { fadeAnimation: false, zoomAnimation: false }).setView([35.14, -79.0], 12);
    notices = [];
    calls = [];
});

afterEach(() => {
    map.remove();
});

describe("createFeatureLayer", () => {
    test("is off, and asks nothing, until toggled", () => {
        const layer = createFeatureLayer(map, CONFIG, (n) => notices.push(n), fetcherReturning(answer()));
        expect(layer.isActive()).toBe(false);
        expect(calls).toEqual([]);
    });

    test("draws each feature in its own pane once toggled on, and shows the endpoint's notice", async () => {
        const layer = createFeatureLayer(map, CONFIG, (n) => notices.push(n), fetcherReturning(answer()));
        layer.toggle();
        await layer.whenLoaded();

        expect(layer.isActive()).toBe(true);
        expect(calls).toEqual([CONFIG.url]);
        expect(drawnPaths()).toBe(1);
        expect(notices).toEqual([{ kind: "info", message: "This pin is inside NORTH RANGE." }]);
    });

    test("an answer without a notice says nothing", async () => {
        const layer = createFeatureLayer(map, CONFIG, (n) => notices.push(n), fetcherReturning(answer({ notice: undefined })));
        layer.toggle();
        await layer.whenLoaded();
        expect(notices).toEqual([]);
        expect(drawnPaths()).toBe(1);
    });

    test("hides without forgetting, and shows again without asking again", async () => {
        const layer = createFeatureLayer(map, CONFIG, (n) => notices.push(n), fetcherReturning(answer()));
        layer.toggle();
        await layer.whenLoaded();
        layer.toggle();
        expect(layer.isActive()).toBe(false);
        expect(drawnPaths()).toBe(0);

        layer.toggle();
        await layer.whenLoaded();
        expect(drawnPaths()).toBe(1);
        expect(calls).toHaveLength(1);
    });

    test("a failure warns, and the next toggle asks again", async () => {
        const layer = createFeatureLayer(map, CONFIG, (n) => notices.push(n), fetcherReturning(new HttpError(503, "Zones are temporarily unavailable."), answer()));
        layer.toggle();
        await layer.whenLoaded();
        expect(notices).toEqual([{ kind: "warning", message: "Zones are temporarily unavailable." }]);
        expect(drawnPaths()).toBe(0);

        layer.toggle();
        layer.toggle();
        await layer.whenLoaded();
        expect(calls).toHaveLength(2);
        expect(drawnPaths()).toBe(1);
    });

    test("an answer that lands after the layer was turned off says nothing", async () => {
        const layer = createFeatureLayer(map, CONFIG, (n) => notices.push(n), fetcherReturning(answer()));
        layer.toggle();
        layer.toggle();
        await layer.whenLoaded();
        expect(notices).toEqual([]);
        expect(drawnPaths()).toBe(0);
    });

    test("marks its own button busy while it loads", async () => {
        const layer = createFeatureLayer(map, CONFIG, (n) => notices.push(n), fetcherReturning(answer()));
        const button = document.querySelector("[data-map-layer=zones]")!;
        layer.toggle();
        expect(button.classList.contains("is-loading")).toBe(true);
        await layer.whenLoaded();
        expect(button.classList.contains("is-loading")).toBe(false);
    });
});

describe("featureStyle", () => {
    test("uses the feature's colour, and a fallback for one without", () => {
        expect(featureStyle(ZONE).color).toBe("#556b2f");
        const bare = featureStyle({ ...ZONE, properties: {} }).color;
        expect(bare).toBeTruthy();
        expect(bare).not.toBe("#556b2f");
    });

    test("fills faintly, so the map beneath stays readable", () => {
        expect(featureStyle(ZONE).fillOpacity).toBeLessThanOrEqual(0.15);
    });
});

describe("featureLabelHtml", () => {
    test("escapes the endpoint's names", () => {
        const html = featureLabelHtml({ label: "Training area", name: "<img src=x>" });
        expect(html).not.toContain("<img");
        expect(html).toContain("Training area");
    });
});

describe("parseFeatureLayers", () => {
    test("reads the embedded list", () => {
        expect(parseFeatureLayers(JSON.stringify([CONFIG]))).toEqual([CONFIG]);
    });

    test("is empty when the attribute is missing, malformed, or not a list", () => {
        expect(parseFeatureLayers(undefined)).toEqual([]);
        expect(parseFeatureLayers("")).toEqual([]);
        expect(parseFeatureLayers("{not json")).toEqual([]);
        expect(parseFeatureLayers('{"key": "zones"}')).toEqual([]);
    });

    test("drops entries that are not layers", () => {
        expect(parseFeatureLayers(JSON.stringify([CONFIG, { key: "x" }, null]))).toEqual([CONFIG]);
    });
});
