/**
 * The street and dark basemaps draw Protomaps' hosted tiles where the page carries the embed, and fall back to the
 * style's own tiles - for every map on the page, and for the rest of the tab's session - when those fail.
 */

import { afterEach, beforeEach, describe, expect, spyOn, test } from "bun:test";
import type { Map as MaplibreMap, SourceSpecification } from "maplibre-gl";

import {
    BASEMAP_SOURCE_ID,
    ERROR_THRESHOLD,
    ERROR_WINDOW_MS,
    FALLBACK_STORAGE_KEY,
    HOSTED_BASEMAP_ATTRIBUTION,
    HOSTED_BASEMAP_EMBED_ID,
    REPROBE_AFTER_MS,
    hostedTilesFor,
    hostedTilesTemplate,
    onHostedBasemapFallback,
    resetHostedBasemapForTests,
    useHostedTiles,
    useHostedTilesOn,
    vectorBaseCredit,
    watchHostedTiles,
} from "./hosted-basemap";

const HOSTED = "https://api.protomaps.com/tiles/v4/{z}/{x}/{y}.mvt?key=pk_test";
const OWN = "https://tiles.urbanlens.org/basemap/{z}/{x}/{y}";

function embed(content: unknown): void {
    const el = document.createElement("script");
    el.type = "application/json";
    el.id = HOSTED_BASEMAP_EMBED_ID;
    el.textContent = typeof content === "string" ? content : JSON.stringify(content);
    document.body.appendChild(el);
}

/** A tile error as MapLibre fires it on the map: an `AJAXError` carries `status` and `url`; a network failure neither. */
function tileError(sourceId: string, status?: number, url = "https://api.protomaps.com/tiles/v4/1/0/0.mvt?key=pk_test"): Record<string, unknown> {
    const error = status === undefined ? new TypeError("Failed to fetch") : Object.assign(new Error(`AJAXError (${status})`), { status, url });
    return { type: "error", error, sourceId };
}

/** Enough of a MapLibre map for the watcher: sources that can be re-pointed, and `error`/`style.load`/`remove` events. */
class FakeGlMap {
    readonly sources = new Map<string, { type: string; tiles?: string[]; url?: string }>();
    readonly setTilesCalls: Array<{ id: string; tiles: string[] }> = [];
    private readonly handlers = new Map<string, Set<(event: unknown) => void>>();
    private readonly onceHandlers = new Map<string, Set<(event: unknown) => void>>();

    addSource(id: string, source: { type: string; tiles?: string[]; url?: string }): void {
        this.sources.set(id, { ...source });
    }
    getSource(id: string): { setTiles(tiles: string[]): void } | undefined {
        const source = this.sources.get(id);
        if (!source) return undefined;
        return {
            setTiles: (tiles: string[]) => {
                this.setTilesCalls.push({ id, tiles });
                source.tiles = tiles;
            },
        };
    }
    getStyle(): { sources: Record<string, unknown> } {
        return { sources: Object.fromEntries([...this.sources].map(([id, source]) => [id, { ...source }])) };
    }
    on(event: string, handler: (event: unknown) => void): void {
        if (!this.handlers.has(event)) this.handlers.set(event, new Set());
        this.handlers.get(event)!.add(handler);
    }
    once(event: string, handler: (event: unknown) => void): void {
        if (!this.onceHandlers.has(event)) this.onceHandlers.set(event, new Set());
        this.onceHandlers.get(event)!.add(handler);
    }
    off(event: string, handler: (event: unknown) => void): void {
        this.handlers.get(event)?.delete(handler);
        this.onceHandlers.get(event)?.delete(handler);
    }
    listenerCount(event: string): number {
        return (this.handlers.get(event)?.size ?? 0) + (this.onceHandlers.get(event)?.size ?? 0);
    }
    fire(event: string, data: unknown = {}): void {
        for (const handler of [...(this.handlers.get(event) ?? [])]) handler(data);
        const once = this.onceHandlers.get(event);
        if (once) {
            this.onceHandlers.set(event, new Set());
            for (const handler of once) handler(data);
        }
    }
    tilesOf(id: string): string[] | undefined {
        return this.sources.get(id)?.tiles;
    }
}

function asMaplibre(map: FakeGlMap): MaplibreMap {
    return map as unknown as MaplibreMap;
}

/** A bridge map whose style has loaded with the basemap source on its own tiles, watched through `useHostedTilesOn`. */
function bridgeMap(kind = "street"): FakeGlMap {
    const map = new FakeGlMap();
    useHostedTilesOn(asMaplibre(map), kind);
    map.addSource(BASEMAP_SOURCE_ID, { type: "vector", tiles: [OWN] });
    map.fire("style.load");
    return map;
}

let warn: ReturnType<typeof spyOn>;
let error: ReturnType<typeof spyOn>;
const realNow = Date.now;

beforeEach(() => {
    resetHostedBasemapForTests();
    sessionStorage.clear();
    warn = spyOn(console, "warn").mockImplementation(() => {});
    error = spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
    warn.mockRestore();
    error.mockRestore();
    Date.now = realNow;
    document.body.innerHTML = "";
    sessionStorage.clear();
    resetHostedBasemapForTests();
});

describe("which tiles a page draws", () => {
    test("without the embed - development, local, or no key - the style's own tiles, untouched", () => {
        expect(hostedTilesTemplate()).toBeNull();

        const sources: Record<string, SourceSpecification> = { protomaps: { type: "vector", tiles: [OWN] } };
        expect(useHostedTiles(sources, "protomaps", "street")).toBeNull();
        expect(sources.protomaps).toEqual({ type: "vector", tiles: [OWN] });

        // Nothing is even listened for on a bridge map.
        const map = new FakeGlMap();
        useHostedTilesOn(asMaplibre(map), "street");
        expect(map.listenerCount("style.load")).toBe(0);
    });

    test("with it, the street and dark basemap source draws the hosted tiles", () => {
        embed({ tiles: HOSTED });
        expect(hostedTilesFor("street")).toBe(HOSTED);
        expect(hostedTilesFor("dark")).toBe(HOSTED);

        const sources: Record<string, SourceSpecification> = { "ul-protomaps": { type: "vector", tiles: [OWN], maxzoom: 15 } };
        expect(useHostedTiles(sources, "ul-protomaps", "street")).toEqual([OWN]);
        expect(sources["ul-protomaps"]).toEqual({ type: "vector", tiles: [HOSTED], maxzoom: 15 });
    });

    test("only the street and dark layers move: terrain is a different dataset", () => {
        embed({ tiles: HOSTED });
        expect(hostedTilesFor("topographic")).toBeNull();
        const sources: Record<string, SourceSpecification> = { protomaps: { type: "vector", tiles: [OWN] } };
        expect(useHostedTiles(sources, "protomaps", "topographic")).toBeNull();
        expect(sources.protomaps).toEqual({ type: "vector", tiles: [OWN] });
    });

    test("leaves a source alone that has no plain tile list to fall back to", () => {
        embed({ tiles: HOSTED });
        const sources: Record<string, SourceSpecification> = {
            protomaps: { type: "vector", url: "pmtiles://https://tiles.urbanlens.org/planet.pmtiles" },
            dem: { type: "raster-dem", tiles: [OWN] },
        };
        expect(useHostedTiles(sources, "protomaps", "street")).toBeNull();
        expect(useHostedTiles(sources, "dem", "street")).toBeNull();
        expect(useHostedTiles(sources, "missing", "street")).toBeNull();
    });

    test.each([
        ["an empty template", { tiles: "" }],
        ["a plain-http template", { tiles: "http://api.protomaps.com/tiles/v4/{z}/{x}/{y}.mvt" }],
        ["a template with no coordinates", { tiles: "https://api.protomaps.com/tiles/v4/" }],
        ["malformed JSON", "{not json"],
    ])("an embed carrying %s is no embed at all", (_label, content) => {
        embed(content);
        expect(hostedTilesTemplate()).toBeNull();
    });

    test("the bridge's map is pointed at the hosted tiles as its style loads, before any tile is asked for", () => {
        embed({ tiles: HOSTED });
        const map = bridgeMap();
        expect(map.setTilesCalls).toEqual([{ id: BASEMAP_SOURCE_ID, tiles: [HOSTED] }]);
        expect(map.tilesOf(BASEMAP_SOURCE_ID)).toEqual([HOSTED]);
    });
});

describe("falling back to our own tiles", () => {
    test.each([401, 403])("one tile refused with %i is enough: a wrong key or origin will refuse every tile", (status) => {
        embed({ tiles: HOSTED });
        const map = bridgeMap();

        map.fire("error", tileError(BASEMAP_SOURCE_ID, status));

        expect(map.tilesOf(BASEMAP_SOURCE_ID)).toEqual([OWN]);
        expect(warn).toHaveBeenCalledTimes(1);
        expect(hostedTilesTemplate()).toBeNull();
    });

    test(`${ERROR_THRESHOLD} failures within the window are, network failures included`, () => {
        embed({ tiles: HOSTED });
        const map = bridgeMap();

        // A refusal arrives without CORS headers, so the browser reports it with no status at all.
        for (let i = 1; i < ERROR_THRESHOLD; i++) map.fire("error", tileError(BASEMAP_SOURCE_ID));
        expect(map.tilesOf(BASEMAP_SOURCE_ID)).toEqual([HOSTED]);
        expect(warn).not.toHaveBeenCalled();

        map.fire("error", tileError(BASEMAP_SOURCE_ID, 503));
        expect(map.tilesOf(BASEMAP_SOURCE_ID)).toEqual([OWN]);
        expect(warn).toHaveBeenCalledTimes(1);
    });

    test("failures spread wider than the window are a flaky tile, not a failed source", () => {
        embed({ tiles: HOSTED });
        const map = bridgeMap();
        let now = 1_000_000;
        Date.now = () => now;

        for (let i = 0; i < ERROR_THRESHOLD * 2; i++) {
            map.fire("error", tileError(BASEMAP_SOURCE_ID, 500));
            now += ERROR_WINDOW_MS / (ERROR_THRESHOLD - 1) + 1;
        }

        expect(map.tilesOf(BASEMAP_SOURCE_ID)).toEqual([HOSTED]);
        expect(warn).not.toHaveBeenCalled();
    });

    test("another source's errors neither count nor go missing", () => {
        embed({ tiles: HOSTED });
        const map = bridgeMap();

        for (let i = 0; i < ERROR_THRESHOLD; i++) map.fire("error", tileError("pins", 403));

        expect(map.tilesOf(BASEMAP_SOURCE_ID)).toEqual([HOSTED]);
        // Listening for `error` stops MapLibre printing them itself, so they are printed as it would have.
        expect(error).toHaveBeenCalledTimes(ERROR_THRESHOLD);
    });

    test("every map on the page falls back together, once, and never swaps back", () => {
        embed({ tiles: HOSTED });
        const first = bridgeMap("street");
        const second = bridgeMap("dark");
        const told: string[] = [];
        onHostedBasemapFallback(() => told.push("fell back"));

        first.fire("error", tileError(BASEMAP_SOURCE_ID, 403));
        // Tiles that were in flight when it swapped still fail afterwards; they must not swap or warn again.
        first.fire("error", tileError(BASEMAP_SOURCE_ID, 403));
        second.fire("error", tileError(BASEMAP_SOURCE_ID, 403));

        expect(first.tilesOf(BASEMAP_SOURCE_ID)).toEqual([OWN]);
        expect(second.tilesOf(BASEMAP_SOURCE_ID)).toEqual([OWN]);
        expect(first.setTilesCalls).toHaveLength(2);
        expect(second.setTilesCalls).toHaveLength(2);
        expect(warn).toHaveBeenCalledTimes(1);
        expect(told).toEqual(["fell back"]);
        // The late hosted failures are swallowed; nothing is printed for them.
        expect(error).not.toHaveBeenCalled();
    });

    test("after the fallback, a failure of our own tiles is reported as MapLibre would have", () => {
        embed({ tiles: HOSTED });
        const map = bridgeMap();
        map.fire("error", tileError(BASEMAP_SOURCE_ID, 403));

        map.fire("error", tileError(BASEMAP_SOURCE_ID, 500, "https://tiles.urbanlens.org/basemap/1/0/0"));

        expect(error).toHaveBeenCalledTimes(1);
        expect(map.tilesOf(BASEMAP_SOURCE_ID)).toEqual([OWN]);
    });

    test("a map whose style arrives after the page fell back starts on our own tiles", () => {
        embed({ tiles: HOSTED });
        const map = new FakeGlMap();
        useHostedTilesOn(asMaplibre(map), "street");
        bridgeMap().fire("error", tileError(BASEMAP_SOURCE_ID, 403));

        map.addSource(BASEMAP_SOURCE_ID, { type: "vector", tiles: [OWN] });
        map.fire("style.load");

        expect(map.setTilesCalls).toEqual([]);
        expect(map.tilesOf(BASEMAP_SOURCE_ID)).toEqual([OWN]);
    });

    test("a map that goes away stops being watched", () => {
        embed({ tiles: HOSTED });
        const map = bridgeMap();
        expect(map.listenerCount("error")).toBe(1);

        map.fire("remove");

        expect(map.listenerCount("error")).toBe(0);
    });

    test("watching directly - the MapLibre engine's path - swaps the source it was given", () => {
        embed({ tiles: HOSTED });
        const map = new FakeGlMap();
        map.addSource("ul-vector-street-protomaps", { type: "vector", tiles: [HOSTED] });
        const stop = watchHostedTiles(asMaplibre(map), "ul-vector-street-protomaps", [OWN]);

        map.fire("error", tileError("ul-vector-street-protomaps", 403));

        expect(map.tilesOf("ul-vector-street-protomaps")).toEqual([OWN]);
        stop();
        expect(map.listenerCount("error")).toBe(0);
    });
});

describe("remembering the fallback for the session", () => {
    function newPage(): void {
        resetHostedBasemapForTests();
    }

    test("a page opened soon after a fallback starts on our own tiles", () => {
        embed({ tiles: HOSTED });
        bridgeMap().fire("error", tileError(BASEMAP_SOURCE_ID, 403));
        expect(sessionStorage.getItem(FALLBACK_STORAGE_KEY)).not.toBeNull();

        newPage();

        expect(hostedTilesTemplate()).toBeNull();
        const map = bridgeMap();
        expect(map.setTilesCalls).toEqual([]);
    });

    test(`a page opened ${REPROBE_AFTER_MS / 60_000} minutes later tries the hosted tiles again`, () => {
        embed({ tiles: HOSTED });
        let now = 5_000_000;
        Date.now = () => now;
        bridgeMap().fire("error", tileError(BASEMAP_SOURCE_ID, 403));

        now += REPROBE_AFTER_MS + 1;
        newPage();

        expect(hostedTilesTemplate()).toBe(HOSTED);
    });

    test("an unreadable entry does not keep the hosted tiles off", () => {
        embed({ tiles: HOSTED });
        sessionStorage.setItem(FALLBACK_STORAGE_KEY, "not a time");
        expect(hostedTilesTemplate()).toBe(HOSTED);
    });

    test("without storage the page still falls back; the next one simply tries again", () => {
        embed({ tiles: HOSTED });
        // What a browser with site data blocked does: the accessor itself throws.
        const real = Object.getOwnPropertyDescriptor(window, "sessionStorage");
        Object.defineProperty(window, "sessionStorage", {
            configurable: true,
            get() {
                throw new Error("SecurityError");
            },
        });
        try {
            const map = bridgeMap();
            map.fire("error", tileError(BASEMAP_SOURCE_ID, 403));
            expect(map.tilesOf(BASEMAP_SOURCE_ID)).toEqual([OWN]);

            newPage();
            expect(hostedTilesTemplate()).toBe(HOSTED);
        } finally {
            if (real) Object.defineProperty(window, "sessionStorage", real);
            else delete (window as unknown as Record<string, unknown>).sessionStorage;
        }
    });

    test("the decision is made once per page, so a fallback expiring mid-page changes nothing", () => {
        embed({ tiles: HOSTED });
        let now = 9_000_000;
        Date.now = () => now;
        sessionStorage.setItem(FALLBACK_STORAGE_KEY, String(now));
        expect(hostedTilesTemplate()).toBeNull();

        now += REPROBE_AFTER_MS + 1;

        expect(hostedTilesTemplate()).toBeNull();
    });
});

describe("the credit", () => {
    const OWN_STYLE = { attribution: "© OpenStreetMap contributors © Protomaps" };

    test("names Protomaps and OpenStreetMap while the hosted tiles are drawn, and the layer's own after a fallback", () => {
        embed({ tiles: HOSTED });
        expect(vectorBaseCredit("street", OWN_STYLE)).toBe(HOSTED_BASEMAP_ATTRIBUTION);
        expect(HOSTED_BASEMAP_ATTRIBUTION).toContain("Protomaps</a> &copy; <a");
        expect(HOSTED_BASEMAP_ATTRIBUTION).toContain("OpenStreetMap");

        bridgeMap().fire("error", tileError(BASEMAP_SOURCE_ID, 403));

        expect(vectorBaseCredit("street", OWN_STYLE)).toBe(OWN_STYLE.attribution);
    });

    test("is the layer's own wherever the hosted tiles are not drawn", () => {
        expect(vectorBaseCredit("street", { attribution: "own" })).toBe("own");
        embed({ tiles: HOSTED });
        resetHostedBasemapForTests();
        expect(vectorBaseCredit("topographic", { attribution: "terrain credit" })).toBe("terrain credit");
    });

    test("a public built-in style keeps its own credit even where the hosted tiles are on: they never replace its tiles", () => {
        embed({ tiles: HOSTED });
        expect(vectorBaseCredit("street", { attribution: "OpenFreeMap © OpenMapTiles", builtIn: true })).toBe("OpenFreeMap © OpenMapTiles");
    });
});
