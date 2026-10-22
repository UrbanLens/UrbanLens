import { beforeEach, describe, expect, mock, test } from "bun:test";

import { fetchCommonsNear, fetchOverpassBuildings, fetchWikipediaNear, installBrowserFetch, MAX_OVERPASS_BYTES, parseOverpassBuildings, readCapped, resetBrowserFetch } from "./browser-fetch";

const GEO = {
    query: {
        geosearch: [
            { pageid: 11, title: "Hudson River State Hospital", lat: 41.73, lon: -73.93, dist: 40 },
            { pageid: "x", title: "Broken", lat: 1, lon: 2 },
            { pageid: 12, title: "Missing coordinates" },
        ],
    },
};

const calls: Array<{ url: string; init?: RequestInit }> = [];
let wikipediaOk = true;

beforeEach(() => {
    calls.length = 0;
    wikipediaOk = true;
    resetBrowserFetch();
    window.csrftoken = "tok";
    window.htmx = { ajax: mock(async () => undefined) } as unknown as typeof window.htmx;
    globalThis.fetch = mock(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({ url, init });
        if (url.startsWith("https://en.wikipedia.org/")) {
            return new Response(JSON.stringify(GEO), { status: wikipediaOk ? 200 : 503 });
        }
        return new Response(JSON.stringify({ stored: 1 }), { status: 200 });
    }) as unknown as typeof fetch;
    document.body.innerHTML = `
        <div class="article-sources">
            <div hidden data-browser-fetch="wikipedia" data-latitude="41.73000" data-longitude="-73.93000"
                 data-post-url="/map/pin/p/browser-fetch/" data-refresh-url="/p/article/sources/?attempt=1"></div>
        </div>`;
});

const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

describe("browser fetch", () => {
    test("reads Wikipedia's geosearch and keeps only well-formed articles", async () => {
        const found = await fetchWikipediaNear("41.73000", "-73.93000");
        expect(found).toEqual([{ title: "Hudson River State Hospital", pageid: 11, lat: 41.73, lon: -73.93 }]);
        expect(calls[0]?.url).toContain("gscoord=41.73000%7C-73.93000");
        expect(calls[0]?.url).toContain("origin=*");
    });

    test("posts the articles back and refreshes the panel", async () => {
        installBrowserFetch();
        await settle();
        const post = calls.find((c) => c.url === "/map/pin/p/browser-fetch/");
        expect(post).toBeDefined();
        expect(JSON.parse(String(post?.init?.body))).toEqual({
            provider: "wikipedia",
            results: [{ title: "Hudson River State Hospital", pageid: 11, lat: 41.73, lon: -73.93 }],
        });
        expect(window.htmx?.ajax).toHaveBeenCalledTimes(1);
    });

    test("posts nothing when Wikipedia does not answer", async () => {
        wikipediaOk = false;
        installBrowserFetch();
        await settle();
        expect(calls.some((c) => c.url.includes("browser-fetch"))).toBe(false);
        expect(window.htmx?.ajax).not.toHaveBeenCalled();
    });

    test("a panel re-rendered with the same request is not fetched twice", async () => {
        installBrowserFetch();
        await settle();
        document.body.dispatchEvent(new CustomEvent("htmx:afterSwap"));
        await settle();
        expect(calls.filter((c) => c.url.startsWith("https://en.wikipedia.org/"))).toHaveLength(1);
    });
});

const COMMONS = {
    query: {
        geosearch: [
            { pageid: 21, title: "File:Mill.jpg", lat: 41.73, lon: -73.93 },
            { pageid: 22, title: "File:No coordinates.jpg" },
        ],
    },
};

const RING = [
    { lat: 41.73, lon: -73.93 },
    { lat: 41.73, lon: -73.9299 },
    { lat: 41.7301, lon: -73.9299 },
    { lat: 41.73, lon: -73.93 },
];

const OVERPASS = {
    elements: [
        { type: "way", id: 7, bounds: { minlat: 41.73, minlon: -73.93, maxlat: 41.7302, maxlon: -73.9298 }, geometry: RING, tags: { name: "Boiler house", "addr:housenumber": "4" } },
        { type: "relation", id: 8, bounds: { minlat: 41.731, minlon: -73.931, maxlat: 41.732, maxlon: -73.93 }, tags: {} },
        { type: "node", id: 9, bounds: { minlat: 1, minlon: 1, maxlat: 1, maxlon: 1 } },
        { type: "way", id: "x", bounds: { minlat: 1, minlon: 1, maxlat: 1, maxlon: 1 } },
        { type: "way", id: 10 },
    ],
};

describe("browser fetch of Commons and Overpass", () => {
    let overpassStatus = 200;
    let overpassBody = JSON.stringify(OVERPASS);

    beforeEach(() => {
        overpassStatus = 200;
        overpassBody = JSON.stringify(OVERPASS);
        globalThis.fetch = mock(async (input: RequestInfo | URL, init?: RequestInit) => {
            const url = String(input);
            calls.push({ url, init });
            if (url.startsWith("https://commons.wikimedia.org/")) return new Response(JSON.stringify(COMMONS), { status: 200 });
            if (url.startsWith("https://overpass-api.de/")) return new Response(overpassBody, { status: overpassStatus });
            return new Response(JSON.stringify({ stored: 1 }), { status: 200 });
        }) as unknown as typeof fetch;
    });

    test("Commons is searched for files, in the file namespace", async () => {
        const found = await fetchCommonsNear("41.73000", "-73.93000");
        expect(found).toEqual([{ title: "File:Mill.jpg", pageid: 21, lat: 41.73, lon: -73.93 }]);
        expect(calls[0]?.url).toContain("gsnamespace=6");
    });

    test("Overpass buildings keep an identity and a centre, and a closed way keeps its outline", () => {
        const found = parseOverpassBuildings(JSON.stringify(OVERPASS));
        expect(found).toHaveLength(2);
        expect(found[0]).toMatchObject({ type: "way", id: 7, name: "Boiler house", ref: "4" });
        expect(found[0]?.ring).toHaveLength(4);
        expect(found[0]?.ring?.[0]).toEqual([-73.93, 41.73]);
        expect(found[1]).toEqual({ type: "relation", id: 8, lat: 41.7315, lon: -73.9305 });
    });

    test("an Overpass answer that is not JSON reads as nothing", () => {
        expect(parseOverpassBuildings("<html>busy</html>")).toEqual([]);
    });

    test("a long outline is thinned to what the server takes, still closed", () => {
        const ring = Array.from({ length: 500 }, (_, i) => ({ lat: 41.73 + i * 1e-6, lon: -73.93 }));
        ring.push({ ...ring[0]! });
        const doc = { elements: [{ type: "way", id: 1, bounds: { minlat: 41.73, minlon: -73.93, maxlat: 41.7305, maxlon: -73.93 }, geometry: ring }] };
        const [only] = parseOverpassBuildings(JSON.stringify(doc));
        expect(only?.ring?.length).toBeLessThanOrEqual(200);
        expect(only?.ring?.[0]).toEqual(only?.ring?.[only.ring.length - 1]);
    });

    test("an oversized body is refused whether or not it declares its length", async () => {
        const big = "x".repeat(MAX_OVERPASS_BYTES + 1);
        expect(await readCapped(new Response(big), MAX_OVERPASS_BYTES)).toBeNull();
        expect(await readCapped(new Response("ok", { headers: { "content-length": String(MAX_OVERPASS_BYTES + 1) } }), MAX_OVERPASS_BYTES)).toBeNull();
        expect(await readCapped(new Response("ok"), MAX_OVERPASS_BYTES)).toBe("ok");
    });

    test("Overpass is asked with the server's outline by POST, and never with anything else in it", async () => {
        const found = await fetchOverpassBuildings("41.730000 -73.930000 41.731000 -73.930000 41.730000 -73.930000");
        expect(found).toHaveLength(2);
        const call = calls.find((c) => c.url.startsWith("https://overpass-api.de/"));
        expect(call?.init?.method).toBe("POST");
        expect(String(call?.init?.body)).toContain("poly%3A%2241.730000");
        calls.length = 0;
        expect(await fetchOverpassBuildings('1 2"); out; ("')).toEqual([]);
        expect(calls).toHaveLength(0);
    });

    test("a refusal or an oversized answer from Overpass posts nothing", async () => {
        overpassStatus = 429;
        document.body.innerHTML = `<div hidden data-browser-fetch="overpass" data-polygon="41.73 -73.93" data-post-url="/p/browser-fetch/" data-refresh-url="/p/buildings/"></div>`;
        installBrowserFetch();
        await settle();
        expect(calls.some((c) => c.url.includes("browser-fetch"))).toBe(false);
    });

    test("Commons files are posted and the gallery asked for what was added", async () => {
        document.body.innerHTML = `<div id="media-gallery-grid"><div hidden data-browser-fetch="wikimedia_commons" data-latitude="41.73000" data-longitude="-73.93000"
            data-post-url="/p/browser-fetch/" data-refresh-url="/p/media/?browser_fetched=1" data-refresh-target="#media-gallery-grid" data-refresh-swap="beforeend"></div></div>`;
        installBrowserFetch();
        await settle();
        const post = calls.find((c) => c.url === "/p/browser-fetch/");
        expect(JSON.parse(String(post?.init?.body))).toEqual({ provider: "wikimedia_commons", results: [{ title: "File:Mill.jpg", pageid: 21, lat: 41.73, lon: -73.93 }] });
        expect(window.htmx?.ajax).toHaveBeenCalledWith("GET", "/p/media/?browser_fetched=1", { target: document.querySelector("#media-gallery-grid"), swap: "beforeend" });
    });

    test("Overpass buildings are posted and the panel replaced", async () => {
        document.body.innerHTML = `<div id="parcel-buildings-section"><div hidden data-browser-fetch="overpass" data-polygon="41.73 -73.93 41.74 -73.93 41.73 -73.93"
            data-post-url="/p/browser-fetch/" data-refresh-url="/p/buildings/" data-refresh-target="#parcel-buildings-section" data-refresh-swap="outerHTML"></div></div>`;
        installBrowserFetch();
        await settle();
        const post = calls.find((c) => c.url === "/p/browser-fetch/");
        expect(JSON.parse(String(post?.init?.body)).provider).toBe("overpass");
        expect(JSON.parse(String(post?.init?.body)).results).toHaveLength(2);
        expect(window.htmx?.ajax).toHaveBeenCalledWith("GET", "/p/buildings/", { target: document.querySelector("#parcel-buildings-section"), swap: "outerHTML" });
    });
});
