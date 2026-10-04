import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import { MarkupEngine, type DrawSessionOpts } from "./markup-engine";
import { createMarkupToolbar } from "./markup-toolbar";
import { wrapFetch } from "./site-runtime";

declare const L: typeof import("leaflet");

const realFetch = globalThis.fetch;
const realL = (globalThis as Record<string, unknown>).L;
const realToastr = window.toastr;

/** What the server answers for a URL; a function so a test can fail one request and not the other. */
let answer: (url: string, init?: RequestInit) => Promise<Response>;
let toasts: string[];
let netReports: string[];
let draw: DrawSessionOpts;

const PANEL = `
    <div class="map-wrapper"><div id="map" style="width: 400px; height: 300px"></div></div>
    <div id="markup-panel" style="display:none">
        <span id="markup-panel-title"></span><span id="markup-panel-hint-text"></span>
        <div id="markup-panel-hint"></div><div id="markup-panel-draw-actions"></div><div id="markup-panel-edit-actions"></div>
        <input id="markup-panel-label" value=""><input id="markup-panel-color" value="#e53e3e"><input id="markup-panel-border" value="">
        <input id="markup-panel-width" value="3"><input id="markup-panel-fill-opacity" value="87"><input id="markup-panel-border-opacity" value="100">
        <input id="markup-panel-security" value="">
        <div id="markup-panel-color-swatches"></div><div id="markup-panel-border-swatches"></div>
    </div>`;

beforeAll(async () => {
    (globalThis as Record<string, unknown>).L = (await import("leaflet")).default;
    window.MarkupEngine = {
        ...MarkupEngine,
        createDrawSession: (map, opts) => {
            draw = opts;
            return MarkupEngine.createDrawSession(map, opts);
        },
    };
});

afterAll(() => {
    globalThis.fetch = realFetch;
    (globalThis as Record<string, unknown>).L = realL;
    window.toastr = realToastr;
});

beforeEach(() => {
    toasts = [];
    netReports = [];
    document.body.innerHTML = PANEL;
    window.toastr = { success: () => undefined, error: (m: string) => void toasts.push(m), warning: (m: string) => void toasts.push(m), info: () => undefined, clear: () => undefined };
    globalThis.fetch = wrapFetch(Object.assign((input: RequestInfo | URL, init?: RequestInit) => answer(String(input), init), realFetch), (m) => void netReports.push(m));
});

/** Draws a circle and waits for the save and the redraw after it to settle. */
async function drawCircle(): Promise<void> {
    const map = L.map("map").setView([42.5, -71.25], 16);
    createMarkupToolbar(map, L.layerGroup().addTo(map), { markupJsonUrl: "/markup/", markupCreateUrl: "/markup/create/", markupEditUrlTemplate: "/markup/00000000-0000-0000-0000-000000000000/" });
    await new Promise((resolve) => setTimeout(resolve, 0));
    draw.onCommit?.("circle", [
        [42.5, -71.25],
        [42.501, -71.25],
    ], {});
    for (let i = 0; i < 10; i++) await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("drawing a new markup item", () => {
    test("a save that worked, followed by a redraw that did not, says the item was saved", async () => {
        let reads = 0;
        answer = async (url, init) => {
            if (init?.method === "POST") return Response.json({ uuid: "new-item" });
            reads += 1;
            // The first read is the toolbar loading what is already there.
            return reads === 1 ? Response.json({ markup_items: [] }) : new Response("<h1>Server Error</h1>", { status: 500 });
        };

        await drawCircle();

        expect(toasts).toEqual(["Markup saved, but the map could not show it. Reload the page to see it."]);
        expect(netReports).toEqual([]);
    });

    test("a save that failed says so", async () => {
        answer = async (url, init) => (init?.method === "POST" ? new Response("", { status: 500 }) : Response.json({ markup_items: [] }));

        await drawCircle();

        expect(toasts).toEqual(["Failed to save markup."]);
        expect(netReports).toEqual([]);
    });
});

describe("pointing the toolbar at another markup URL", () => {
    const item = (uuid: string) => ({ uuid, markup_type: "marker", geometry: { type: "Point", coordinates: [-71.25, 42.5] }, label: uuid, color: "#e53e3e" });

    function toolbarOnMap() {
        const map = L.map("map").setView([42.5, -71.25], 16);
        return createMarkupToolbar(map, L.layerGroup().addTo(map), { markupJsonUrl: "/markup/", markupCreateUrl: "/markup/create/", markupEditUrlTemplate: "/markup/00000000-0000-0000-0000-000000000000/" });
    }

    test("the next load reads the new URL", async () => {
        const reads: string[] = [];
        answer = async (url) => {
            reads.push(url);
            return Response.json({ markup_items: [] });
        };
        const toolbar = toolbarOnMap();
        await new Promise((resolve) => setTimeout(resolve, 0));

        toolbar.setMarkupJsonUrl("/markup/?children=1");
        toolbar.loadMarkup();
        await new Promise((resolve) => setTimeout(resolve, 0));

        expect(reads).toEqual(["/markup/", "/markup/?children=1"]);
    });

    test("an answer for the old URL that arrives late does not replace the new one's items", async () => {
        let releaseOld: () => void = () => undefined;
        answer = async (url) => {
            if (url === "/markup/") await new Promise<void>((resolve) => (releaseOld = resolve));
            return Response.json({ markup_items: [item(url === "/markup/" ? "parent-only" : "with-children")] });
        };
        const toolbar = toolbarOnMap();

        toolbar.setMarkupJsonUrl("/markup/?children=1");
        toolbar.loadMarkup();
        for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0));
        releaseOld();
        for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0));

        expect(toolbar.getMarkupItems().map((markup) => markup.uuid)).toEqual(["with-children"]);
    });
});
