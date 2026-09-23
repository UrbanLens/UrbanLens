import { afterEach, describe, expect, test } from "bun:test";

import { DEFAULT_SCHEDULE, PROCESSING_LABEL, ProcessingPoller, pollDelay, processingPlaceholder, processingStateOf, watchProcessingTiles } from "./photo-processing";
import { renderPhotoTile, tileFromJson, tileHasImage } from "./photo-tile";
import { renderVaultPhotoTile } from "./vault-photo-grid";

afterEach(() => {
    document.body.innerHTML = "";
    window.urbanlensProcessingPollers?.forEach((poller) => poller.stop());
    window.urbanlensProcessingPollers?.clear();
});

const PENDING = { id: 7, url: "", thumb_url: "", caption: "Boiler room", processing: true, processing_failed: false };
const FAILED = { ...PENDING, processing: false, processing_failed: true };
const READY = { ...PENDING, processing: false, url: "https://m.test/media/pin_images/a/p.webp", thumb_url: "https://m.test/media/pin_images/t/p.webp" };

function statusResponse(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function attached(el: HTMLElement): HTMLElement {
    document.body.append(el);
    return el;
}

describe("processing state", () => {
    test("reads the two server flags", () => {
        expect(processingStateOf(PENDING)).toBe("pending");
        expect(processingStateOf(FAILED)).toBe("failed");
        expect(processingStateOf(READY)).toBe("");
    });

    test("the placeholder is labelled and requests nothing", () => {
        const span = processingPlaceholder("photo-tile-fallback");
        expect(span.getAttribute("role")).toBe("img");
        expect(span.getAttribute("aria-label")).toBe(PROCESSING_LABEL);
        expect(span.classList.contains("media-processing")).toBe(true);
        expect(span.querySelector("img")).toBeNull();
        expect(processingPlaceholder("photo-tile-fallback", true).classList.contains("media-processing")).toBe(false);
    });
});

describe("vault tile", () => {
    test("a processing photo renders a placeholder and no image", () => {
        const li = renderVaultPhotoTile(PENDING);
        expect(li).not.toBeNull();
        expect(li?.dataset.processing).toBe("pending");
        expect(li?.querySelector("img")).toBeNull();
        expect(li?.querySelector('[role="img"]')?.getAttribute("aria-label")).toBe(PROCESSING_LABEL);
        expect(li?.querySelector<HTMLButtonElement>(".photo-tile-btn")?.disabled).toBe(true);
        expect(li?.querySelector(".photo-tile-del")).not.toBeNull();
    });

    test("a failed photo renders the failed placeholder", () => {
        const li = renderVaultPhotoTile(FAILED);
        expect(li?.dataset.processing).toBe("failed");
        expect(li?.querySelector("img")).toBeNull();
        expect(li?.querySelector(".media-processing")).toBeNull();
    });

    test("a ready photo renders its thumbnail", () => {
        const li = renderVaultPhotoTile(READY);
        expect(li?.hasAttribute("data-processing")).toBe(false);
        expect(li?.querySelector("img")?.getAttribute("src")).toBe(READY.thumb_url);
    });
});

describe("album tile", () => {
    test("a processing photo is kept, as a placeholder with no image", () => {
        const tile = tileFromJson(PENDING);
        if (!tile) throw new Error("unparseable");
        expect(tileHasImage(tile)).toBe(true);
        const li = renderPhotoTile(tile, { inAlbum: false });
        expect(li.dataset.processing).toBe("pending");
        expect(li.querySelector("img")).toBeNull();
        expect(li.querySelector('[role="img"]')?.getAttribute("aria-label")).toBe(PROCESSING_LABEL);
    });
});

describe("poller", () => {
    test("backs off to a ceiling", () => {
        expect(pollDelay(0)).toBe(DEFAULT_SCHEDULE.initialMs);
        expect(pollDelay(1)).toBe(DEFAULT_SCHEDULE.initialMs * 2);
        expect(pollDelay(50)).toBe(DEFAULT_SCHEDULE.maxMs);
    });

    test("one request covers every watched photo, and only settled ones are handed back", async () => {
        const urls: string[] = [];
        const poller = new ProcessingPoller("/vault/photos/processing/", { initialMs: 60000, maxMs: 60000, maxPolls: 5 }, async (url) => {
            urls.push(url);
            return statusResponse({ items: [READY], processing: [8] });
        });
        const settled: Array<[number, unknown]> = [];
        poller.watch(7, attached(document.createElement("li")), (item) => settled.push([7, item]));
        poller.watch(8, attached(document.createElement("li")), (item) => settled.push([8, item]));
        poller.watch(9, attached(document.createElement("li")), (item) => settled.push([9, item]));
        await poller.poll();
        expect(urls).toEqual(["/vault/photos/processing/?ids=7,8,9"]);
        expect(settled).toEqual([
            [7, READY],
            [9, null],
        ]);
        expect(poller.size).toBe(1);
        poller.stop();
    });

    test("a failed request keeps waiting", async () => {
        const poller = new ProcessingPoller("/s/", { initialMs: 60000, maxMs: 60000, maxPolls: 5 }, async () => statusResponse({}, 502));
        let calls = 0;
        poller.watch(7, attached(document.createElement("li")), () => calls++);
        await poller.poll();
        expect(calls).toBe(0);
        expect(poller.size).toBe(1);
        poller.stop();
    });

    test("a tile removed from the page is no longer asked about", async () => {
        const urls: string[] = [];
        const poller = new ProcessingPoller("/s/", { initialMs: 60000, maxMs: 60000, maxPolls: 5 }, async (url) => {
            urls.push(url);
            return statusResponse({ items: [], processing: [8] });
        });
        const gone = attached(document.createElement("li"));
        poller.watch(7, gone, () => undefined);
        poller.watch(8, attached(document.createElement("li")), () => undefined);
        gone.remove();
        await poller.poll();
        expect(urls).toEqual(["/s/?ids=8"]);
        poller.stop();
    });

    test("a tile removed while its poll is in flight is not settled", async () => {
        let release: (response: Response) => void = () => undefined;
        const poller = new ProcessingPoller("/s/", { initialMs: 60000, maxMs: 60000, maxPolls: 5 }, () => new Promise<Response>((resolve) => (release = resolve)));
        const stale = attached(document.createElement("li"));
        const live = attached(document.createElement("li"));
        const settled: string[] = [];
        poller.watch(7, stale, () => settled.push("stale"));
        poller.watch(7, live, () => settled.push("live"));
        const polling = poller.poll();
        stale.remove();
        release(statusResponse({ items: [], processing: [] }));
        await polling;
        expect(settled).toEqual(["live"]);
        poller.stop();
    });

    test("stops after its poll budget", async () => {
        let calls = 0;
        const poller = new ProcessingPoller("/s/", { initialMs: 1, maxMs: 1, maxPolls: 3 }, async () => {
            calls++;
            return statusResponse({ items: [], processing: [7] });
        });
        poller.watch(7, attached(document.createElement("li")), () => undefined);
        await new Promise((resolve) => setTimeout(resolve, 60));
        expect(calls).toBe(3);
        expect(poller.size).toBe(1);
        poller.stop();
    });

    test("stops once nothing is left to wait for", async () => {
        let calls = 0;
        const poller = new ProcessingPoller("/s/", { initialMs: 1, maxMs: 1, maxPolls: 10 }, async () => {
            calls++;
            return statusResponse({ items: [READY], processing: [] });
        });
        poller.watch(7, attached(document.createElement("li")), () => undefined);
        await new Promise((resolve) => setTimeout(resolve, 40));
        expect(calls).toBe(1);
        expect(poller.size).toBe(0);
    });
});

describe("watching tiles", () => {
    test("a tile finds its status URL on an ancestor, and one poll per URL is shared", () => {
        document.body.innerHTML = `<ul id="grid" data-processing-url="/vault/photos/processing/"></ul>`;
        const grid = document.getElementById("grid");
        if (!grid) throw new Error("missing grid");
        const pending = renderVaultPhotoTile(PENDING);
        const ready = renderVaultPhotoTile({ ...READY, id: 8 });
        if (!pending || !ready) throw new Error("unrendered");
        grid.append(pending, ready);
        watchProcessingTiles(grid, () => undefined);
        watchProcessingTiles(grid, () => undefined);
        const poller = window.urbanlensProcessingPollers?.get("/vault/photos/processing/");
        expect(poller?.size).toBe(1);
    });
});
