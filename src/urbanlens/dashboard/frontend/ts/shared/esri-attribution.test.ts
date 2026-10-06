/**
 * Esri's basemaps are credited by the providers of the area on screen. The fixture is Esri's own
 * contributor data around Albany, NY, so the expectations below are what a viewer there is shown.
 */
import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import albany from "../testing/esri-attribution-albany.json";
import {
    composeAttribution,
    contributorsFor,
    esriCoverages,
    esriServiceForUrl,
    isEsriServiceName,
    parseContributors,
    POWERED_BY_ESRI,
    resetEsriAttributionForTests,
    RETRY_AFTER_FAILURE_MS,
    seedEsriCoveragesForTests,
    setEsriAttributionFetchForTests,
    settledForTests,
    type AttributionView,
} from "./esri-attribution";

/** Roughly what an 800x500 dialog shows of Albany at zoom 15. */
function albanyAt(zoom: number): AttributionView {
    const halfLng = (400 * 360) / (256 * 2 ** zoom);
    const halfLat = halfLng * 0.46;
    return { south: 42.6526 - halfLat, west: -73.7562 - halfLng, north: 42.6526 + halfLat, east: -73.7562 + halfLng, zoom };
}

const imagery = parseContributors(albany.World_Imagery);
const topo = parseContributors(albany.World_Topo_Map);

beforeEach(() => resetEsriAttributionForTests());
afterEach(() => {
    resetEsriAttributionForTests();
    setEsriAttributionFetchForTests(() => Promise.reject(new Error("unit tests do not reach static.arcgis.com")));
});

describe("contributorsFor", () => {
    test("satellite over Albany at zoom 15 credits the state's orthophotos and Vantor, not the world's list", () => {
        expect(contributorsFor(imagery, albanyAt(15))).toEqual(["New York State", "Vantor"]);
    });

    test("topographic over Albany at zoom 15 credits the providers Esri lists for that area", () => {
        expect(contributorsFor(topo, albanyAt(15))).toEqual(["Esri Canada", "Esri", "HERE", "Garmin", "INCREMENT P", "USGS", "METI/NASA", "EPA", "USDA"]);
    });

    test("follows the zoom: zoomed out, the global mosaic is what is drawn", () => {
        expect(contributorsFor(imagery, albanyAt(10))).toEqual(["Earthstar Geographics"]);
    });

    test("names each provider once, however many of its areas the view touches", () => {
        const names = contributorsFor(imagery, { south: 41, west: -80, north: 46, east: -73, zoom: 13 });
        expect(names.filter((name) => name === "New York State")).toHaveLength(1);
    });

    test("leaves out an area the view does not reach", () => {
        // Vantor's z14-17 coverage starts at 42.07N.
        expect(contributorsFor(imagery, { south: 30, west: -74, north: 31, east: -73, zoom: 15 })).not.toContain("Vantor");
    });

    test("finds coverage on the far side of the antimeridian", () => {
        const coverages = parseContributors({ contributors: [{ attribution: "East", coverageAreas: [{ score: 1, zoomMin: 0, zoomMax: 20, bbox: [-10, 175, 10, 179] }] }] });
        // A view from 170E to 190E (-170) crosses the line.
        expect(contributorsFor(coverages, { south: -1, west: 170, north: 1, east: 190, zoom: 5 })).toEqual(["East"]);
        expect(contributorsFor(coverages, { south: -1, west: -185, north: 1, east: -178, zoom: 5 })).toEqual(["East"]);
    });
});

describe("parseContributors", () => {
    test("orders by score, highest first, keeping the service's order on a tie", () => {
        const parsed = parseContributors({
            contributors: [
                { attribution: "Low", coverageAreas: [{ score: 10, zoomMin: 0, zoomMax: 20, bbox: [0, 0, 1, 1] }] },
                { attribution: "High", coverageAreas: [{ score: 90, zoomMin: 0, zoomMax: 20, bbox: [0, 0, 1, 1] }] },
                { attribution: "AlsoLow", coverageAreas: [{ score: 10, zoomMin: 0, zoomMax: 20, bbox: [0, 0, 1, 1] }] },
            ],
        });
        expect(parsed.map((coverage) => coverage.attribution)).toEqual(["High", "Low", "AlsoLow"]);
    });

    test("drops entries that are not shaped like coverage, rather than throwing", () => {
        const parsed = parseContributors({
            contributors: [
                { attribution: "", coverageAreas: [{ score: 1, zoomMin: 0, zoomMax: 1, bbox: [0, 0, 1, 1] }] },
                { attribution: "No bbox", coverageAreas: [{ score: 1, zoomMin: 0, zoomMax: 1 }] },
                { attribution: "Bad zoom", coverageAreas: [{ score: 1, zoomMin: "x", zoomMax: 1, bbox: [0, 0, 1, 1] }] },
                { attribution: "Fine", coverageAreas: [{ score: 1, zoomMin: 0, zoomMax: 1, bbox: [0, 0, 1, 1] }] },
                null,
            ],
        });
        expect(parsed.map((coverage) => coverage.attribution)).toEqual(["Fine"]);
        expect(parseContributors(null)).toEqual([]);
        expect(parseContributors({ contributors: "nope" })).toEqual([]);
    });
});

describe("esriServiceForUrl", () => {
    test("reads the service, folder included, from an ArcGIS Online template", () => {
        expect(esriServiceForUrl("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}")).toBe("World_Imagery");
        expect(esriServiceForUrl("https://services.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}")).toBe(
            "Reference/World_Boundaries_and_Places",
        );
    });

    test("is null for anyone else's tiles, and for this deployment's proxy", () => {
        expect(esriServiceForUrl("https://tile.openweathermap.org/map/precipitation_new/{z}/{x}/{y}.png")).toBeNull();
        expect(esriServiceForUrl("/dashboard/map/basemap-tiles/satellite/{z}/{x}/{y}/")).toBeNull();
    });

    test("a catalogue value only counts as a service name if it is one", () => {
        expect(isEsriServiceName("Canvas/World_Dark_Gray_Base")).toBe(true);
        expect(isEsriServiceName("../../evil")).toBe(false);
        expect(isEsriServiceName("World_Imagery?x=1")).toBe(false);
        expect(isEsriServiceName(7)).toBe(false);
    });
});

describe("composeAttribution", () => {
    const satellite = { kind: "esri" as const, service: "World_Imagery", fallback: "Esri, Maxar, Earthstar Geographics, and the GIS User Community", maxNativeZoom: 19 };

    test("shows the static credit, after Esri's own, until the providers for the view are known", () => {
        expect(composeAttribution([satellite], albanyAt(15), "Leaflet")).toBe(`${POWERED_BY_ESRI} · Esri, Maxar, Earthstar Geographics, and the GIS User Community · Leaflet`);
    });

    test("names only the view's providers once they are", () => {
        seedEsriCoveragesForTests("World_Imagery", imagery);
        expect(composeAttribution([satellite], albanyAt(15), "Leaflet")).toBe("Powered by Esri · New York State, Vantor · Leaflet");
    });

    test("credits past the tiles' own depth as the depth they are enlarged from", () => {
        seedEsriCoveragesForTests("World_Imagery", imagery);
        // Zoom 21 draws zoom 19's tiles, which are Microsoft's and Vantor's here.
        expect(composeAttribution([satellite], albanyAt(21), "")).toBe("Powered by Esri · New York State, Microsoft, Vantor");
    });

    test("says Powered by Esri once however many Esri layers are drawn, and credits each layer's own providers", () => {
        seedEsriCoveragesForTests("World_Imagery", imagery);
        seedEsriCoveragesForTests("Reference/World_Boundaries_and_Places", parseContributors({ contributors: [{ attribution: "HERE", coverageAreas: [{ score: 1, zoomMin: 0, zoomMax: 23, bbox: [-85, -180, 85, 180] }] }] }));
        const text = composeAttribution([satellite, { kind: "esri", service: "Reference/World_Boundaries_and_Places", fallback: "Esri" }], albanyAt(15), "Leaflet");
        expect(text).toBe("Powered by Esri · New York State, Vantor · HERE · Leaflet");
    });

    test("credits a non-Esri base without Esri's line", () => {
        expect(composeAttribution([{ kind: "text", text: "OpenFreeMap © OpenMapTiles Data from OpenStreetMap" }], albanyAt(15), "MapLibre")).toBe("OpenFreeMap © OpenMapTiles Data from OpenStreetMap · MapLibre");
    });

    test("leaves the renderer off an exported image", () => {
        expect(composeAttribution([{ kind: "text", text: "© OSM" }], null, "")).toBe("© OSM");
    });
});

describe("esriCoverages", () => {
    function answer(body: unknown, ok = true): { calls: string[] } {
        const seen = { calls: [] as string[] };
        setEsriAttributionFetchForTests((url) => {
            seen.calls.push(url);
            return Promise.resolve({ ok, json: () => Promise.resolve(body) } as Response);
        });
        return seen;
    }

    test("asks Esri's attribution file for the service once, and tells each waiter once it arrives", async () => {
        const seen = answer(albany.World_Imagery);
        let ready = 0;
        const onReady = (): void => void ready++;

        expect(esriCoverages("World_Imagery", onReady)).toBeNull();
        expect(esriCoverages("World_Imagery", onReady)).toBeNull();
        await settledForTests("World_Imagery");

        expect(seen.calls).toEqual(["https://static.arcgis.com/attribution/World_Imagery"]);
        expect(ready).toBe(1);
        expect(esriCoverages("World_Imagery")).toHaveLength(imagery.length);
    });

    test("does not keep a failure as an answer, but waits before asking again", async () => {
        const seen = answer({}, false);
        expect(esriCoverages("World_Topo_Map")).toBeNull();
        await settledForTests("World_Topo_Map");
        expect(seen.calls).toHaveLength(1);

        // A pan straight after the failure does not ask again.
        expect(esriCoverages("World_Topo_Map")).toBeNull();
        expect(seen.calls).toHaveLength(1);

        const realNow = Date.now;
        Date.now = () => realNow() + RETRY_AFTER_FAILURE_MS + 1;
        try {
            answer(albany.World_Topo_Map);
            esriCoverages("World_Topo_Map");
            await settledForTests("World_Topo_Map");
            expect(esriCoverages("World_Topo_Map")).toHaveLength(topo.length);
        } finally {
            Date.now = realNow;
        }
    });

    test("asks without cookies or a referrer, and reports its own failure rather than toasting it", async () => {
        let sent: (RequestInit & { __ulReported?: boolean }) | undefined;
        setEsriAttributionFetchForTests((_url, init) => {
            sent = init;
            return Promise.resolve({ ok: true, json: () => Promise.resolve(albany.World_Imagery) } as Response);
        });
        esriCoverages("World_Street_Map");
        await settledForTests("World_Street_Map");
        expect(sent).toMatchObject({ credentials: "omit", referrerPolicy: "no-referrer", __ulReported: true });
    });

    test("never builds a request from something that is not a service name", () => {
        const seen = answer(albany.World_Imagery);
        expect(esriCoverages("../../oops")).toBeNull();
        expect(seen.calls).toEqual([]);
    });
});
