/**
 * The manage-overlays dialog body (``partials/layout/_map_overlays_list.html``) names its controls in markup; the
 * page that owns the map wires them once, and they keep working through every htmx re-render of the body.
 */

import { afterAll, afterEach, beforeAll, describe, expect, it } from "bun:test";

import { wireManageOverlaysDialog } from "./map-image-overlays";

const calls: unknown[][] = [];
const control = {
    setVisible: (...args: unknown[]) => void calls.push(["setVisible", ...args]),
    startAlign: (...args: unknown[]) => void calls.push(["startAlign", ...args]),
    stopAlign: () => void calls.push(["stopAlign"]),
    previewOpacity: (...args: unknown[]) => void calls.push(["previewOpacity", ...args]),
};
const point = (lat: number, lng: number) => ({ lat, lng });
const bounds = { getNorthWest: () => point(2, 0), getNorthEast: () => point(2, 1), getSouthEast: () => point(1, 1), getSouthWest: () => point(1, 0) };
const host = document.createElement("div");
const map = { getContainer: () => host, getBounds: () => ({ pad: () => bounds }) };
const fetched: string[] = [];
const realFetch = globalThis.fetch;

const BODY = `
  <ul id="map-overlay-manage-list">
    <li class="map-overlay-manage-row" data-overlay-uuid="ov-1">
      <input type="range" name="opacity" value="80">
      <button type="button" data-overlay-align="ov-1"><i>open_with</i></button>
    </li>
  </ul>
  <form id="map-overlay-add-form">
    <label id="map-overlay-dropzone"><span id="map-overlay-dropzone-text"></span><input type="file" name="image"></label>
    <input type="url" name="image_url">
    <button type="button" id="map-overlay-pick-media-btn" data-gallery-url="/pins/p/gallery.json"><i>photo_library</i></button>
    <div id="map-overlay-media-picker" hidden></div>
    <input type="hidden" name="image_id" id="map-overlay-image-id" value="9">
    <p id="map-overlay-picked-media">Using: Plan</p>
    <input type="hidden" name="corners" id="map-overlay-initial-corners">
    <button type="submit" id="map-overlay-add-submit" disabled>Add overlay</button>
  </form>`;

let aligned = 0;

beforeAll(() => {
    globalThis.fetch = (async (input: RequestInfo | URL) => {
        fetched.push(String(input));
        return new Response(JSON.stringify({ images: [] }));
    }) as typeof fetch;
    wireManageOverlaysDialog({ map: map as unknown as L.Map, control: control as unknown as Parameters<typeof wireManageOverlaysDialog>[0]["control"], onAlignStart: () => aligned++ });
});

afterAll(() => {
    globalThis.fetch = realFetch;
});

afterEach(() => {
    calls.length = 0;
    fetched.length = 0;
    document.body.innerHTML = "";
});

function mount(): void {
    document.body.innerHTML = BODY;
}

function fire(selector: string, event: Event): Event {
    document.querySelector(selector)?.dispatchEvent(event);
    return event;
}

describe("the manage-overlays dialog", () => {
    it("previews a row's opacity as its slider moves", () => {
        mount();
        (document.querySelector('input[name="opacity"]') as HTMLInputElement).value = "40";
        fire('input[name="opacity"]', new Event("input", { bubbles: true }));
        expect(calls).toEqual([["previewOpacity", "ov-1", 40]]);
    });

    it("starts aligning the overlay a row's Align button names", () => {
        mount();
        const before = aligned;
        fire("[data-overlay-align] i", new MouseEvent("click", { bubbles: true }));
        expect(calls).toEqual([
            ["setVisible", "ov-1", true],
            ["startAlign", "ov-1"],
        ]);
        expect(aligned).toBe(before + 1);
    });

    it("lights the drop zone while a file is dragged over it", () => {
        mount();
        const over = fire("#map-overlay-dropzone-text", new Event("dragover", { bubbles: true, cancelable: true }));
        expect(over.defaultPrevented).toBe(true);
        expect(document.getElementById("map-overlay-dropzone")?.classList.contains("is-dragover")).toBe(true);
        fire("#map-overlay-dropzone", new Event("dragleave", { bubbles: true }));
        expect(document.getElementById("map-overlay-dropzone")?.classList.contains("is-dragover")).toBe(false);
    });

    it("a typed image address replaces a picked photo and enables Add", () => {
        mount();
        (document.querySelector('input[name="image_url"]') as HTMLInputElement).value = "https://example.test/plan.jpg";
        fire('input[name="image_url"]', new Event("input", { bubbles: true }));
        expect((document.getElementById("map-overlay-image-id") as HTMLInputElement).value).toBe("");
        expect(document.getElementById("map-overlay-picked-media")?.hidden).toBe(true);
        expect((document.getElementById("map-overlay-add-submit") as HTMLButtonElement).disabled).toBe(false);
    });

    it("choosing a file drops the typed address and the picked photo", () => {
        mount();
        (document.querySelector('input[name="image_url"]') as HTMLInputElement).value = "https://example.test/plan.jpg";
        fire('input[type="file"]', new Event("change", { bubbles: true }));
        expect((document.querySelector('input[name="image_url"]') as HTMLInputElement).value).toBe("");
        expect((document.getElementById("map-overlay-image-id") as HTMLInputElement).value).toBe("");
    });

    it("Choose from uploaded photos opens the picker on this page's gallery", async () => {
        mount();
        fire("#map-overlay-pick-media-btn i", new MouseEvent("click", { bubbles: true }));
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(document.getElementById("map-overlay-media-picker")?.hidden).toBe(false);
        expect(fetched).toEqual(["/pins/p/gallery.json"]);
    });
});
