/**
 * The Private Pin map's Land Use layer: off until toggled, fetched once, drawn beneath the page's own shapes, and
 * explained when there is nothing to draw.
 */

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { HttpError } from "./fetch-json";
import { createLandUseLayer, LAND_USE_PANE, type LandUseAreasResponse, landUseLabelHtml, type LandUseNotice, landUseNotice, landUseStyle } from "./land-use-layer";

declare const L: typeof import("leaflet");

const realL = (globalThis as Record<string, unknown>).L;
const URL = "/dashboard/pin/mill/land-use-areas/json/";

const FORT: LandUseAreasResponse["features"][number] = {
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
    properties: { category: "military_installation", label: "Military installation", name: "FORT LIBERTY" },
};

function answer(overrides: Partial<LandUseAreasResponse> = {}): LandUseAreasResponse {
    return { type: "FeatureCollection", features: [FORT], status: "found", complete: true, ...overrides };
}

let map: L.Map;
let notices: LandUseNotice[];
let calls: string[];

function fetcherReturning(...results: (LandUseAreasResponse | Error)[]) {
    return async <T>(url: string): Promise<T | null> => {
        calls.push(url);
        const next = results.shift();
        if (next instanceof Error) throw next;
        return (next ?? null) as T | null;
    };
}

function drawnPaths(): number {
    return map.getPane(LAND_USE_PANE)?.querySelectorAll("path").length ?? 0;
}

beforeAll(async () => {
    (globalThis as Record<string, unknown>).L = (await import("leaflet")).default;
});

afterAll(() => {
    (globalThis as Record<string, unknown>).L = realL;
});

beforeEach(() => {
    document.body.innerHTML = '<div id="map" style="width: 400px; height: 300px"></div><button data-map-layer="landuse"></button>';
    map = L.map("map", { fadeAnimation: false, zoomAnimation: false }).setView([35.14, -79.0], 12);
    notices = [];
    calls = [];
});

afterEach(() => {
    map.remove();
});

describe("createLandUseLayer", () => {
    test("is off, and asks nothing, until toggled", () => {
        const layer = createLandUseLayer(map, URL, (n) => notices.push(n), fetcherReturning(answer()));
        expect(layer.isActive()).toBe(false);
        expect(calls).toEqual([]);
    });

    test("draws each area in its own pane once toggled on, and names them", async () => {
        const layer = createLandUseLayer(map, URL, (n) => notices.push(n), fetcherReturning(answer()));
        layer.toggle();
        await layer.whenLoaded();

        expect(layer.isActive()).toBe(true);
        expect(calls).toEqual([URL]);
        expect(drawnPaths()).toBe(1);
        expect(notices).toHaveLength(1);
        expect(notices[0]?.message).toContain("FORT LIBERTY");
    });

    test("hides without forgetting, and shows again without asking again", async () => {
        const layer = createLandUseLayer(map, URL, (n) => notices.push(n), fetcherReturning(answer()));
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
        const layer = createLandUseLayer(map, URL, (n) => notices.push(n), fetcherReturning(new HttpError(503, "Land-use boundaries are temporarily unavailable."), answer()));
        layer.toggle();
        await layer.whenLoaded();
        expect(notices).toEqual([{ kind: "warning", message: "Land-use boundaries are temporarily unavailable." }]);
        expect(drawnPaths()).toBe(0);

        layer.toggle();
        layer.toggle();
        await layer.whenLoaded();
        expect(calls).toHaveLength(2);
        expect(drawnPaths()).toBe(1);
    });

    test("an answer that lands after the layer was turned off says nothing", async () => {
        const layer = createLandUseLayer(map, URL, (n) => notices.push(n), fetcherReturning(answer()));
        layer.toggle();
        layer.toggle();
        await layer.whenLoaded();
        expect(notices).toEqual([]);
        expect(drawnPaths()).toBe(0);
    });

    test("marks its button busy while it loads", async () => {
        const layer = createLandUseLayer(map, URL, (n) => notices.push(n), fetcherReturning(answer()));
        const button = document.querySelector("[data-map-layer=landuse]")!;
        layer.toggle();
        expect(button.classList.contains("is-loading")).toBe(true);
        await layer.whenLoaded();
        expect(button.classList.contains("is-loading")).toBe(false);
    });
});

describe("landUseNotice", () => {
    test("names the areas found", () => {
        const campus = { ...FORT, properties: { category: "college_university", label: "College or university", name: "STATE U" } };
        const notice = landUseNotice(answer({ features: [FORT, campus] }));
        expect(notice?.kind).toBe("info");
        expect(notice?.message).toContain("FORT LIBERTY (Military installation)");
        expect(notice?.message).toContain("STATE U (College or university)");
    });

    test("warns when REData drew fewer areas than the parcel is in", () => {
        expect(landUseNotice(answer({ complete: false }))?.kind).toBe("warning");
    });

    test("explains every empty answer", () => {
        for (const status of ["none", "no_parcel", "unavailable", "outside_coverage"] as const) {
            const notice = landUseNotice(answer({ status, features: [] }));
            expect(notice?.kind).toBe("info");
            expect(notice?.message.length).toBeGreaterThan(0);
        }
    });
});

describe("landUseStyle", () => {
    test("gives each category its own colour, and an unknown one a fallback", () => {
        const colours = ["military_installation", "correctional_facility", "national_park", "college_university", "tribal_trust_land"].map(
            (category) => landUseStyle({ ...FORT, properties: { ...FORT.properties, category } }).color,
        );
        expect(new Set(colours).size).toBe(5);
    });

    test("fills faintly, so the map beneath stays readable", () => {
        expect(landUseStyle(FORT).fillOpacity).toBeLessThanOrEqual(0.15);
    });
});

describe("landUseLabelHtml", () => {
    test("escapes REData's names", () => {
        const html = landUseLabelHtml({ category: "national_park", label: "National park", name: "<img src=x>" });
        expect(html).not.toContain("<img");
        expect(html).toContain("National park");
    });
});
