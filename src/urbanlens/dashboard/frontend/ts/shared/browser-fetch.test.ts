import { beforeEach, describe, expect, mock, test } from "bun:test";

import { fetchWikipediaNear, installBrowserFetch, resetBrowserFetch } from "./browser-fetch";

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
