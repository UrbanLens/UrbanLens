import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import type { GalleryMarkerImage } from "../types/globals";

declare const L: typeof import("leaflet");

const realFetch = globalThis.fetch;
const realL = (globalThis as Record<string, unknown>).L;

interface Call {
    url: string;
    body: Record<string, unknown>;
    resolve: (response: Response) => void;
}

let calls: Call[] = [];
let toasts: string[] = [];
let applied: [HTMLElement | undefined, Record<string, unknown>][] = [];
let added: GalleryMarkerImage[] = [];
let map: L.Map;
let placeMediaItem: typeof import("./media-map-drop").placeMediaItem;

const ITEM = { source: "wikimedia", key: "File:Mill.jpg", url: "https://upload.example/Mill.jpg", pageUrl: "https://commons.example/Mill", caption: "The mill" };

beforeAll(async () => {
    const leaflet = await import("leaflet");
    (globalThis as Record<string, unknown>).L = leaflet.default;
    ({ placeMediaItem } = await import("./media-map-drop"));
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL, init?: RequestInit) => {
        return new Promise<Response>((resolve) => calls.push({ url: String(input), body: JSON.parse(String(init?.body)), resolve }));
    }, realFetch);
});

afterAll(() => {
    globalThis.fetch = realFetch;
    (globalThis as Record<string, unknown>).L = realL;
});

beforeEach(() => {
    calls = [];
    toasts = [];
    applied = [];
    added = [];
    document.body.innerHTML = `
        <div id="map" style="width: 400px; height: 300px"></div>
        <div class="media-item" data-media-thumb="/media/thumb/mill.jpg"></div>`;
    map = L.map("map").setView([42.5, -71.25], 16);
    (window as { toastr?: unknown }).toastr = { success: () => {}, info: () => {}, warning: (m: string) => toasts.push(m), error: (m: string) => toasts.push(m) };
    window.mediaApplyMaterializedDrop = (itemEl, data) => void applied.push([itemEl, data]);
    window._galleryAddMarker = (img) => void added.push(img);
});

const tile = (): HTMLElement => document.querySelector<HTMLElement>(".media-item")!;
const pendingMarkers = (): HTMLElement[] => [...map.getPane("markerPane")!.querySelectorAll<HTMLElement>(".media-processing")];

async function settle(placing: Promise<void>, response: Response): Promise<void> {
    for (let attempt = 0; attempt < 50 && !calls.length; attempt++) await new Promise((resolve) => setTimeout(resolve, 1));
    calls[0]!.resolve(response);
    await placing;
}

describe("placing a Media tile on the map", () => {
    test("shows the tile and the drop point as pending until the server has saved it", async () => {
        const placing = placeMediaItem(map, "/pin/media-relevance/", tile(), ITEM, L.latLng(42.51, -71.26));
        expect(tile().classList.contains("media-processing")).toBe(true);
        expect(tile().getAttribute("aria-busy")).toBe("true");
        const [marker] = pendingMarkers();
        expect(marker?.querySelector("img")?.getAttribute("src")).toBe("/media/thumb/mill.jpg");

        const saved = { image_id: 9, image_url: "/media/image/9/", latitude: 42.51, longitude: -71.26 };
        await settle(placing, Response.json(saved));
        expect(calls[0]!.body).toEqual({
            source: "wikimedia",
            item_key: "File:Mill.jpg",
            url: "https://upload.example/Mill.jpg",
            is_relevant: true,
            page_url: "https://commons.example/Mill",
            caption: "The mill",
            latitude: 42.51,
            longitude: -71.26,
        });
        expect(tile().classList.contains("media-processing")).toBe(false);
        expect(tile().hasAttribute("aria-busy")).toBe(false);
        expect(pendingMarkers()).toEqual([]);
        expect(applied).toEqual([[tile(), saved]]);
        expect(added).toEqual([{ id: 9, url: "/media/image/9/", latitude: 42.51, longitude: -71.26 }]);
    });

    test("clears the pending state and says so when the save fails", async () => {
        await settle(placeMediaItem(map, "/pin/media-relevance/", tile(), ITEM, L.latLng(42.51, -71.26)), new Response("nope", { status: 500 }));
        expect(toasts).toEqual(["Failed to save photo location."]);
        expect(tile().classList.contains("media-processing")).toBe(false);
        expect(pendingMarkers()).toEqual([]);
        expect(added).toEqual([]);
    });

    test("a failed local copy is reported once, by the gallery when it is there", async () => {
        const unsaved = { materialize_error: "too large", latitude: 42.51, longitude: -71.26 };
        await settle(placeMediaItem(map, "/pin/media-relevance/", tile(), ITEM, L.latLng(42.51, -71.26)), Response.json(unsaved));
        expect(applied).toEqual([[tile(), unsaved]]);
        expect(toasts).toEqual([]);

        window.mediaApplyMaterializedDrop = undefined;
        calls = [];
        await settle(placeMediaItem(map, "/pin/media-relevance/", tile(), ITEM, L.latLng(42.51, -71.26)), Response.json(unsaved));
        expect(toasts).toEqual(["Couldn't save a local copy: too large"]);
    });

    test("without a tile to borrow a thumbnail from, the drop point still shows it is pending", async () => {
        const placing = placeMediaItem(map, "/pin/media-relevance/", undefined, ITEM, L.latLng(42.51, -71.26));
        const [marker] = pendingMarkers();
        expect(marker?.querySelector("img")).toBeNull();
        expect(marker?.textContent).toContain("hourglass_top");
        expect(document.body.innerHTML).not.toContain(ITEM.url);
        await settle(placing, Response.json({ image_id: 9, image_url: "/media/image/9/", latitude: 42.51, longitude: -71.26 }));
        expect(pendingMarkers()).toEqual([]);
        expect(added).toHaveLength(1);
    });
});
