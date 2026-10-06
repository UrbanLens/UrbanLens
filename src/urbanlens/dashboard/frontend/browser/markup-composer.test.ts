/**
 * The map composer in a real browser: drawing, picking, deleting, reshaping, undo, turning the map
 * and the download it makes. The pure parts are tested on their own (markup-document, markup-geometry,
 * esri-attribution); this is everything that needs Leaflet, pointer events and a canvas.
 *
 * Served by Bun from the built bundles, with the real dialog partial spliced into the page, so the
 * markup under test is the template's own. No database and no Django: saving answers from here.
 */

import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { type Browser, type BrowserContext, type Page, chromium } from "playwright";

// See floorplan-editor.test.ts: the happy-dom preload is wrong for a file that drives a real browser.
GlobalRegistrator.unregister();

const ROOT = join(import.meta.dir, "../../../../..");
const STATIC_DIR = join(ROOT, "src/urbanlens/dashboard/frontend/static");
const CORE = join(STATIC_DIR, "dashboard/js/core.js");
const STYLE = join(STATIC_DIR, "dashboard/style.css");
const PARTIAL = join(ROOT, "src/urbanlens/dashboard/templates/dashboard/partials/map/_markup_composer_dialog.html");
const VENDOR_ASSETS = join(ROOT, "src/urbanlens/dashboard/services/core/vendor_assets.py");
const ALBANY = JSON.parse(readFileSync(join(ROOT, "src/urbanlens/dashboard/frontend/ts/testing/esri-attribution-albany.json"), "utf8")) as Record<string, unknown>;

/** These drive the built bundle and stylesheet, so they need `bun run build` and `bun run sass` to have run. */
const BUILT = existsSync(CORE) && existsSync(STYLE);

/** Where to write the dialog's screenshots, when wanted. They use real tiles, so they need the network. */
const SHOTS_DIR = process.env.UL_COMPOSER_SHOTS_DIR || "";

const ALBANY_VIEW = { lat: 42.6526, lng: -73.7562, zoom: 15 };

/** leaflet-rotate's address and hash, read from the vendor table the real page uses. */
function leafletRotateSource(): { src: string; integrity: string } {
    const table = readFileSync(VENDOR_ASSETS, "utf8");
    const entry = table.slice(table.indexOf('"leaflet_rotate_js"'));
    const [src, integrity] = [...entry.matchAll(/"(https:\/\/[^"]+|sha384-[^"]+)"/g)].slice(0, 2).map((m) => m[1]!);
    return { src: src!, integrity: integrity! };
}

/** A one-pixel GIF of one colour - drawn 256px wide, a tile of that colour. */
function solidGif(r: number, g: number, b: number): Uint8Array<ArrayBuffer> {
    const gif = Uint8Array.from(atob("R0lGODlhAQABAIAAAP///wAAACwAAAAAAQABAAACAkQBADs="), (c) => c.charCodeAt(0));
    gif[13] = r;
    gif[14] = g;
    gif[15] = b;
    return gif;
}

const TILE_COLORS: Record<string, [number, number, number]> = {
    satellite: [40, 120, 40],
    terrain: [200, 180, 120],
    street: [230, 230, 230],
    dark: [30, 30, 40],
    borders: [0, 0, 0],
};

/** The dialog partial as Django renders it, with the two inclusion tags it uses written out. */
function dialogMarkup(): string {
    const searchBar = `<div id="cmcc-search-bar" class="addr-search-bar input-wrap addr-search-bar--composer">
    <i class="material-icons addr-search-icon">place</i>
    <input type="text" id="cmcc-search-input" class="addr-search-input" placeholder="Search pins, addresses, coordinates..." autocomplete="off" spellcheck="false">
    <button type="button" class="addr-search-clear" id="cmcc-search-clear" aria-label="Clear search"><i class="material-symbols-outlined">close</i></button>
    <div id="cmcc-search-suggestions" class="addr-search-suggestions" hidden></div></div>`;
    const strip = `<div class="map-layers-strip " id="cmc-layers" data-map-layers-panel data-default-base="satellite" role="group" aria-label="Map layers">
    ${[
        ["street", "base", "map", "Street map"],
        ["satellite", "base", "globe", "Satellite map (S)"],
        ["terrain", "base", "terrain", "Terrain map (T)"],
        ["borders", "overlay", "border_outer", "Borders"],
    ]
        .map(([key, kind, icon, tip]) => `<button type="button" class="map-layers-strip-btn" id="cmc-layers-${key}" data-map-layer="${key}" data-layer-kind="${kind}" title="${tip}" aria-label="${tip}"><i class="material-symbols-outlined">${icon}</i></button>`)
        .join("\n")}</div>`;
    return readFileSync(PARTIAL, "utf8")
        .replace(/\{%\s*load[^%]*%\}/g, "")
        .replace(/\{#.*?#\}/g, "")
        .replace(/\{%\s*map_search_bar[^%]*%\}/, searchBar)
        .replace(/\{%\s*map_layers_panel[^%]*%\}/, strip);
}

interface Saved {
    bodies: Array<Record<string, unknown>>;
}

const saved: Saved = { bodies: [] };

let browser: Browser;
let server: ReturnType<typeof Bun.serve>;

function pageHtml(params: URLSearchParams): string {
    const harness = readFileSync(join(import.meta.dir, "composer-harness.html"), "utf8");
    const rotate = params.get("norotate") ? null : leafletRotateSource();
    const config = {
        profileUuid: "",
        leafletRotate: rotate,
        urls: {
            commentsImagePicker: "/picker/",
            mapAutocompleteLocal: "/autocomplete/local/",
            mapAutocompletePlaces: "/autocomplete/places/",
            mapResolvePlace: "/resolve/",
            markupMapCreate: "/markup-maps/create/",
            markupMapSnapshot: "/markup-maps/11111111-1111-1111-1111-111111111111/snapshot/",
            messagesAttachMapPicker: "/maps/picker/",
        },
    };
    // Real vendor tiles for a screenshot; this server's solid ones otherwise, so a test never waits on a CDN.
    const catalogue = params.get("realtiles")
        ? ""
        : `<script id="ul-basemap-tiles" type="application/json">${JSON.stringify(
              Object.keys(TILE_COLORS).map((id) => ({
                  id,
                  source_type: "raster",
                  url_template: `/tiles/${id}/{z}/{x}/{y}.gif`,
                  attribution: id === "satellite" ? "Esri, Maxar, Earthstar Geographics, and the GIS User Community" : `${id} credit`,
                  esri_service: id === "satellite" ? "World_Imagery" : id === "terrain" ? "World_Topo_Map" : undefined,
                  max_zoom: 19,
              })),
          )}</script>`;
    return harness
        .replace("__THEME__", params.get("theme") === "dark" ? ' data-theme="dark"' : ' data-theme="light"')
        .replace("__CATALOGUE__", catalogue)
        .replace("__DIALOG__", dialogMarkup())
        .replace("__CONFIG__", JSON.stringify(config));
}

beforeAll(async () => {
    if (!BUILT) return;
    const libs = join(process.env.HOME || "", "browserlibs/root/usr/lib/x86_64-linux-gnu");
    process.env.LD_LIBRARY_PATH = process.env.LD_LIBRARY_PATH ? `${process.env.LD_LIBRARY_PATH}:${libs}` : libs;
    browser = await chromium.launch({ args: ["--no-sandbox", "--disable-gpu"] });
    server = Bun.serve({
        port: 0,
        async fetch(request) {
            const url = new URL(request.url);
            const path = url.pathname;
            if (path === "/") return new Response(pageHtml(url.searchParams), { headers: { "content-type": "text/html" } });
            const tile = /^\/tiles\/([a-z]+)\/\d+\/\d+\/\d+\.gif$/.exec(path);
            if (tile) return new Response(solidGif(...(TILE_COLORS[tile[1]!] ?? [128, 128, 128])), { headers: { "content-type": "image/gif" } });
            if (path === "/markup-maps/create/") {
                saved.bodies.push((await request.json()) as Record<string, unknown>);
                return Response.json({ ok: true, uuid: "22222222-2222-2222-2222-222222222222" });
            }
            if (path === "/dashboard/map/basemap-tiles/sources/") return Response.json({ layers: [] });
            const file = Bun.file(join(STATIC_DIR, path.replace(/^\//, "")));
            if (!(await file.exists())) return new Response("not found", { status: 404 });
            return new Response(file);
        },
    });
});

afterAll(async () => {
    await browser?.close();
    server?.stop(true);
});

interface Opened {
    context: BrowserContext;
    page: Page;
    errors: string[];
}

/** Opens the composer as the main map's "Take a screenshot" button does, over Albany at zoom 15. */
async function openComposer(options: { query?: string; touch?: boolean; existing?: unknown; viewport?: { width: number; height: number } } = {}): Promise<Opened> {
    const context = await browser.newContext({ viewport: options.viewport ?? { width: 1280, height: 900 }, hasTouch: !!options.touch });
    const page = await context.newPage();
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    // Esri's attribution file, as it answers for Albany - the test never reaches the real one.
    await page.route("https://static.arcgis.com/attribution/**", (route) => {
        const service = new URL(route.request().url()).pathname.split("/attribution/")[1] ?? "";
        const body = ALBANY[service];
        return body ? route.fulfill({ json: body }) : route.fulfill({ status: 404, body: "" });
    });
    await page.goto(`${server.url}${options.query ?? ""}`);
    await page.evaluate(
        ({ view, existing }) => {
            if (existing) {
                window._openCommentMapComposer({ existingData: existing });
                return;
            }
            const map = { getCenter: () => ({ lat: view.lat, lng: view.lng }), getZoom: () => view.zoom };
            (window as unknown as { _openMapToolbarScreenshot: (map: unknown, context: unknown) => void })._openMapToolbarScreenshot(map, null);
        },
        { view: ALBANY_VIEW, existing: options.existing ?? null },
    );
    await page.waitForSelector("#comment-map-composer[open] .leaflet-container .leaflet-tile-loaded", { timeout: 20000 });
    // The save button comes back once the composer has been seeded.
    await page.waitForFunction(() => !(document.getElementById("comment-map-composer-save") as HTMLButtonElement).disabled);
    return { context, page, errors };
}

/** The map element's box, for pointer positions on it. */
async function mapBox(page: Page): Promise<{ x: number; y: number; width: number; height: number }> {
    const box = await page.locator("#comment-map-composer-map").boundingBox();
    if (!box) throw new Error("the map has no box");
    return box;
}

/** Draws an arrow with a mouse drag, from left of centre to right of it. */
async function drawArrow(page: Page): Promise<{ from: { x: number; y: number }; to: { x: number; y: number } }> {
    await page.click('.cmc-tool-btn[data-tool="arrow"]');
    const box = await mapBox(page);
    const from = { x: box.x + box.width * 0.3, y: box.y + box.height * 0.5 };
    const to = { x: box.x + box.width * 0.6, y: box.y + box.height * 0.5 };
    await page.mouse.move(from.x, from.y);
    await page.mouse.down();
    for (let step = 1; step <= 10; step++) await page.mouse.move(from.x + ((to.x - from.x) * step) / 10, from.y);
    await page.mouse.up();
    // One shape per arm: put the tool down so a click picks rather than draws.
    await page.click('.cmc-tool-btn[data-tool="arrow"]');
    return { from, to };
}

/** Lets the dialog settle: picking a shape changes the bars under the map, and the map resizes with them. */
async function settle(page: Page): Promise<void> {
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(resolve)))));
}

async function buttonStates(page: Page): Promise<{ undo: boolean; redo: boolean; clear: boolean }> {
    return page.evaluate(() => ({
        undo: !(document.getElementById("cmc-undo") as HTMLButtonElement).disabled,
        redo: !(document.getElementById("cmc-redo") as HTMLButtonElement).disabled,
        clear: !(document.getElementById("cmc-clear") as HTMLButtonElement).disabled,
    }));
}

async function layerNames(page: Page): Promise<string[]> {
    return page.locator("#cmc-layers-list .cmc-layer-label").allTextContents();
}

/** Saves the map and returns what the server was sent. */
async function save(page: Page): Promise<Record<string, unknown>> {
    const before = saved.bodies.length;
    // Answer "no download" to the after-save question.
    await page.evaluate(() => {
        window.confirmDialog = () => Promise.resolve(false);
    });
    await page.click("#comment-map-composer-save");
    for (let i = 0; i < 100 && saved.bodies.length === before; i++) await Bun.sleep(50);
    if (saved.bodies.length === before) throw new Error("the composer never saved");
    // The dialog closes after a save; open it again on the same map so a test can carry on.
    const body = saved.bodies.at(-1)!;
    await page.waitForFunction(() => !(document.getElementById("comment-map-composer") as HTMLDialogElement).open);
    await page.evaluate((existing) => window._openCommentMapComposer({ existingData: existing }), body);
    await page.waitForFunction(() => !(document.getElementById("comment-map-composer-save") as HTMLButtonElement).disabled);
    // Two frames, so the reopened map has laid itself out before a test presses on it.
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    return body;
}

describe.skipIf(!BUILT)("the map composer in a browser", () => {
    test("Undo and Clear start disabled and follow the drawing; an arrow is picked with a click and deleted with the Delete key", async () => {
        const { context, page, errors } = await openComposer();
        expect(await buttonStates(page)).toEqual({ undo: false, redo: false, clear: false });
        expect(await page.locator("#cmc-layers-empty").isVisible()).toBe(true);

        const { from, to } = await drawArrow(page);
        expect(await layerNames(page)).toEqual(["Arrow 1"]);
        expect(await buttonStates(page)).toEqual({ undo: true, redo: false, clear: true });

        // Click the arrow's shaft to pick it.
        await page.mouse.click((from.x + to.x) / 2, from.y);
        await page.waitForSelector(".markup-selection-outline");
        expect(await page.locator(".cmc-layer-row.is-selected .cmc-layer-label").textContent()).toBe("Arrow 1");
        expect(await page.locator("#cmc-style-target").textContent()).toBe("Arrow 1");

        await page.keyboard.press("Delete");
        expect(await layerNames(page)).toEqual([]);
        expect(await page.locator(".markup-selection-outline").count()).toBe(0);
        expect(await buttonStates(page)).toEqual({ undo: true, redo: false, clear: false });

        // Undo brings it back, and Redo takes it away again.
        await page.click("#cmc-undo");
        expect(await layerNames(page)).toEqual(["Arrow 1"]);
        expect(await buttonStates(page)).toEqual({ undo: true, redo: true, clear: true });
        await page.click("#cmc-redo");
        expect(await layerNames(page)).toEqual([]);

        // Undo all the way back: nothing left to undo.
        await page.click("#cmc-undo");
        await page.click("#cmc-undo");
        expect(await layerNames(page)).toEqual([]);
        expect(await buttonStates(page)).toEqual({ undo: false, redo: true, clear: false });

        expect(errors).toEqual([]);
        await context.close();
    });

    test("Backspace typed into a text field edits the field, not the drawing; Escape lets go of the selection and leaves the dialog open", async () => {
        const { context, page } = await openComposer();
        const { from, to } = await drawArrow(page);
        await page.mouse.click((from.x + to.x) / 2, from.y);
        await page.waitForSelector(".markup-selection-outline");

        await page.focus("#cmc-title-input");
        await page.keyboard.type("Ab");
        await page.keyboard.press("Backspace");
        expect(await page.inputValue("#cmc-title-input")).toContain("A");
        expect(await layerNames(page)).toEqual(["Arrow 1"]);

        await page.locator("#comment-map-composer-map").focus();
        await page.keyboard.press("Escape");
        expect(await page.locator(".markup-selection-outline").count()).toBe(0);
        expect(await page.evaluate(() => (document.getElementById("comment-map-composer") as HTMLDialogElement).open)).toBe(true);
        await context.close();
    });

    test("keys stay in the dialog: Escape in a field keeps it open, Ctrl+Z never reaches the page's undo, and a key mid-drag leaves the drag one step", async () => {
        const { context, page } = await openComposer();
        // The page's own undo - the server-side stack behind the dialog - as a spy.
        await page.evaluate(() => {
            const w = window as unknown as { __siteUndo: string[]; ulUndo?: { register: (p: unknown) => void } };
            w.__siteUndo = [];
            w.ulUndo?.register({ canUndo: () => true, canRedo: () => true, undo: () => void w.__siteUndo.push("undo"), redo: () => void w.__siteUndo.push("redo") });
        });
        const siteUndo = (): Promise<string[]> => page.evaluate(() => (window as unknown as { __siteUndo: string[] }).__siteUndo);
        const dialogOpen = (): Promise<boolean> => page.evaluate(() => (document.getElementById("comment-map-composer") as HTMLDialogElement).open);

        const { from, to } = await drawArrow(page);
        await page.locator("#comment-map-composer-map").focus();
        for (let i = 0; i < 3; i++) await page.keyboard.press("Control+z");
        for (let i = 0; i < 3; i++) await page.keyboard.press("Control+Shift+z");
        expect(await layerNames(page)).toEqual(["Arrow 1"]);
        expect(await siteUndo()).toEqual([]);

        // Escape typed into a field leaves the field, not the dialog - closing would lose the drawing.
        await page.focus("#cmc-title-input");
        await page.keyboard.press("Escape");
        expect(await dialogOpen()).toBe(true);
        expect(await layerNames(page)).toEqual(["Arrow 1"]);

        // Ctrl+Z and Delete pressed in the middle of a drag: the drag is still one undo step.
        const mid = { x: (from.x + to.x) / 2, y: from.y };
        await page.mouse.click(mid.x, mid.y);
        await page.waitForSelector(".markup-selection-outline");
        await page.mouse.move(mid.x, mid.y);
        await page.mouse.down();
        for (let i = 1; i <= 5; i++) {
            await page.mouse.move(mid.x, mid.y + i * 6);
            await settle(page);
        }
        await page.keyboard.press("Control+z");
        await page.keyboard.press("Delete");
        for (let i = 6; i <= 10; i++) {
            await page.mouse.move(mid.x, mid.y + i * 6);
            await settle(page);
        }
        await page.mouse.up();
        await settle(page);
        expect(await layerNames(page)).toEqual(["Arrow 1"]);
        await page.click("#cmc-undo");
        await page.click("#cmc-undo");
        expect(await layerNames(page)).toEqual([]);
        expect(await buttonStates(page)).toMatchObject({ undo: false });

        // Held arrow keys are one nudge, not one step per repeat.
        await page.click("#cmc-redo");
        await page.click("#cmc-redo");
        await page.click('.cmc-layer-row >> nth=0 >> button[data-layer-action="select"]');
        await page.locator("#comment-map-composer-map").focus();
        for (let i = 0; i < 30; i++) await page.keyboard.press("ArrowRight");
        await page.keyboard.press("Escape");
        await page.click("#cmc-undo");
        await page.click("#cmc-undo");
        await page.click("#cmc-undo");
        expect(await layerNames(page)).toEqual([]);
        expect(await siteUndo()).toEqual([]);
        await context.close();
    });

    test("the Layers list picks, hides, reorders and deletes, and a hidden shape is not saved", async () => {
        const { context, page } = await openComposer();
        await drawArrow(page);
        // A second shape: a rectangle dragged out below the arrow.
        await page.click('.cmc-tool-btn[data-tool="rect"]');
        const box = await mapBox(page);
        await page.mouse.move(box.x + box.width * 0.35, box.y + box.height * 0.65);
        await page.mouse.down();
        for (let step = 1; step <= 8; step++) await page.mouse.move(box.x + box.width * (0.35 + 0.02 * step), box.y + box.height * (0.65 + 0.015 * step));
        await page.mouse.up();
        await page.click('.cmc-tool-btn[data-tool="rect"]');
        expect(await layerNames(page)).toEqual(["Rectangle 1", "Arrow 1"]);

        await page.click('.cmc-layer-row[data-markup-id] >> nth=1 >> button[data-layer-action="up"]');
        expect(await layerNames(page)).toEqual(["Arrow 1", "Rectangle 1"]);

        await page.click('.cmc-layer-row >> nth=1 >> button[data-layer-action="select"]');
        expect(await page.locator("#cmc-style-target").textContent()).toBe("Rectangle 1");
        expect(await page.locator("#cmc-fill-wrap").isVisible()).toBe(true);

        await page.click('.cmc-layer-row >> nth=1 >> button[data-layer-action="visibility"]');
        expect(await page.locator(".cmc-layer-row.is-hidden").count()).toBe(1);
        expect(await page.locator("#cmc-layers-hidden-note").isVisible()).toBe(true);

        const body = await save(page);
        const markup = body.markup as Array<{ type: string }>;
        expect(markup.map((shape) => shape.type)).toEqual(["arrow"]);
        await context.close();
    });

    test("an arrow is extended, a point is added and removed, and the colour and width are the selection's", async () => {
        const { context, page } = await openComposer();
        const { from, to } = await drawArrow(page);
        await page.mouse.click((from.x + to.x) / 2, from.y);
        await page.waitForSelector(".markup-handle--extend");
        await settle(page);

        // Pull the end's "+" further right: a third point.
        const extend = (await page.locator(".markup-handle--extend").nth(1).boundingBox())!;
        await page.mouse.move(extend.x + extend.width / 2, extend.y + extend.height / 2);
        await page.mouse.down();
        for (let step = 1; step <= 6; step++) await page.mouse.move(extend.x + extend.width / 2 + step * 8, extend.y + extend.height / 2 - step * 6);
        await page.mouse.up();
        expect(await page.locator(".markup-handle--vertex").count()).toBe(3);

        // Tap the middle of the first edge: a fourth.
        await settle(page);
        const mid = (await page.locator(".markup-handle--insert").first().boundingBox())!;
        await page.mouse.click(mid.x + mid.width / 2, mid.y + mid.height / 2);
        expect(await page.locator(".markup-handle--vertex").count()).toBe(4);

        // The added point is picked; Delete removes that point, not the arrow.
        expect(await page.locator(".markup-handle--vertex.is-active").count()).toBe(1);
        await page.keyboard.press("Delete");
        expect(await page.locator(".markup-handle--vertex").count()).toBe(3);
        expect(await layerNames(page)).toEqual(["Arrow 1"]);

        // Colour and width change the picked arrow.
        await page.locator("#cmc-color").evaluate((input: HTMLInputElement) => {
            input.value = "#2196f3";
            input.dispatchEvent(new Event("input", { bubbles: true }));
            input.dispatchEvent(new Event("change", { bubbles: true }));
        });
        await page.locator("#cmc-width").evaluate((input: HTMLInputElement) => {
            input.value = "9";
            input.dispatchEvent(new Event("input", { bubbles: true }));
            input.dispatchEvent(new Event("change", { bubbles: true }));
        });
        const body = await save(page);
        const [arrow] = body.markup as Array<{ type: string; latlngs: number[][]; color: string; stroke_width: number }>;
        expect(arrow).toMatchObject({ type: "arrow", color: "#2196f3", stroke_width: 9 });
        expect(arrow!.latlngs).toHaveLength(3);
        await context.close();
    });

    test("a shape is moved by dragging it and resized by its corner handle", async () => {
        const { context, page } = await openComposer();
        await page.click('.cmc-tool-btn[data-tool="rect"]');
        const box = await mapBox(page);
        const a = { x: box.x + box.width * 0.4, y: box.y + box.height * 0.4 };
        await page.mouse.move(a.x, a.y);
        await page.mouse.down();
        for (let step = 1; step <= 8; step++) await page.mouse.move(a.x + step * 10, a.y + step * 8);
        await page.mouse.up();
        await page.click('.cmc-tool-btn[data-tool="rect"]');
        const before = (await save(page)).markup as Array<{ latlngs: number[][] }>;

        await page.mouse.click(a.x + 40, a.y + 32);
        await page.waitForSelector(".markup-handle--scale");
        await settle(page);
        // Drag the body 60px left.
        await page.mouse.move(a.x + 40, a.y + 32);
        await page.mouse.down();
        for (let step = 1; step <= 6; step++) await page.mouse.move(a.x + 40 - step * 10, a.y + 32);
        await page.mouse.up();
        const moved = (await save(page)).markup as Array<{ latlngs: number[][] }>;
        const lngShift = moved[0]!.latlngs[0]![1]! - before[0]!.latlngs[0]![1]!;
        expect(lngShift).toBeLessThan(0);
        expect(Math.abs(moved[0]!.latlngs[0]![0]! - before[0]!.latlngs[0]![0]!)).toBeLessThan(1e-6);

        // Saving closed and reopened the dialog; pick the moved rectangle again.
        await page.mouse.click(a.x - 20, a.y + 32);
        await page.waitForSelector(".markup-handle--scale");
        await settle(page);
        const scale = (await page.locator(".markup-handle--scale").boundingBox())!;
        await page.mouse.move(scale.x + scale.width / 2, scale.y + scale.height / 2);
        await page.mouse.down();
        for (let step = 1; step <= 6; step++) await page.mouse.move(scale.x + scale.width / 2 + step * 10, scale.y + scale.height / 2 + step * 8);
        await page.mouse.up();
        const grown = (await save(page)).markup as Array<{ latlngs: number[][] }>;
        const span = (shape: { latlngs: number[][] }): number => Math.abs(shape.latlngs[1]![1]! - shape.latlngs[0]![1]!);
        expect(span(grown[0]!)).toBeGreaterThan(span(moved[0]!) * 1.3);
        await context.close();
    });

    test("a label placed without words opens for typing; its words, size and turn are saved", async () => {
        const { context, page } = await openComposer();
        await page.click('.cmc-tool-btn[data-tool="text"]');
        const box = await mapBox(page);
        await page.mouse.click(box.x + box.width * 0.5, box.y + box.height * 0.3);
        await page.waitForSelector(".markup-selection-outline");
        // Focused and selected for typing over.
        expect(await page.evaluate(() => document.activeElement?.id)).toBe("cmc-text-label");
        await page.keyboard.type("Fence gap");
        await page.keyboard.press("Enter");
        expect(await layerNames(page)).toEqual(["Fence gap"]);

        await page.locator("#cmc-rotation").evaluate((input: HTMLInputElement) => {
            input.value = "30";
            input.dispatchEvent(new Event("input", { bubbles: true }));
            input.dispatchEvent(new Event("change", { bubbles: true }));
        });
        expect(await page.locator(".markup-text-label").getAttribute("style")).toContain("rotate(30.0deg)");
        await page.locator("#cmc-width").evaluate((input: HTMLInputElement) => {
            input.value = "28";
            input.dispatchEvent(new Event("input", { bubbles: true }));
            input.dispatchEvent(new Event("change", { bubbles: true }));
        });
        const body = await save(page);
        expect((body.markup as unknown[])[0]).toMatchObject({ type: "text", label: "Fence gap", rotation: 30, stroke_width: 28 });
        await context.close();
    });

    test("on a touchscreen a tap picks a shape and the Delete button removes it", async () => {
        const { context, page } = await openComposer({ touch: true, viewport: { width: 420, height: 860 } });
        const { from, to } = await drawArrow(page);
        await page.touchscreen.tap((from.x + to.x) / 2, from.y);
        await page.waitForSelector("#cmc-delete-selected:not([hidden])");
        await page.tap("#cmc-delete-selected");
        expect(await layerNames(page)).toEqual([]);
        await context.close();
    });

    test("a map saved before any of this loads, and saves in the current shape", async () => {
        const legacy = {
            center_lat: ALBANY_VIEW.lat,
            center_lng: ALBANY_VIEW.lng,
            zoom: 15,
            layer_mode: "satellite",
            show_borders: false,
            markup: [
                // `weight` and {lat, lng} points, as an older client wrote them.
                { type: "line", latlngs: [{ lat: 42.652, lng: -73.757 }, { lat: 42.653, lng: -73.755 }], color: "#e74c3c", weight: 5 },
                { type: "text", latlngs: [[42.6526, -73.7562]], color: "#e53e3e", stroke_width: 16, label: "Old label" },
                { type: "bogus", latlngs: [[1, 2]] },
            ],
        };
        const { context, page } = await openComposer({ existing: legacy });
        expect(await layerNames(page)).toEqual(["Old label", "Line 1"]);
        const body = await save(page);
        expect(body.bearing).toBe(0);
        expect(body.markup).toEqual([
            { type: "line", latlngs: [[42.652, -73.757], [42.653, -73.755]], color: "#e74c3c", stroke_width: 5 },
            { type: "text", latlngs: [[42.6526, -73.7562]], color: "#e53e3e", stroke_width: 16, label: "Old label" },
        ]);
        await context.close();
    });

    test("the map turns, the turn is saved, and the download is drawn turned with the tiles' credit in it", async () => {
        const { context, page } = await openComposer();
        await page.waitForSelector(".cmc-rotate-control");
        // A rectangle square to the screen before the turn.
        await page.click('.cmc-tool-btn[data-tool="rect"]');
        const box = await mapBox(page);
        const center = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
        await page.mouse.move(center.x - 120, center.y - 20);
        await page.mouse.down();
        for (let step = 1; step <= 8; step++) await page.mouse.move(center.x - 120 + step * 30, center.y - 20 + step * 5);
        await page.mouse.up();
        await page.click('.cmc-tool-btn[data-tool="rect"]');

        // Three presses clockwise: the content turns 45 degrees, so up is now north-west.
        for (let i = 0; i < 3; i++) await page.click('.cmc-rotate-btn[aria-label="Turn the map clockwise"]');
        expect(await page.locator(".cmc-rotate-compass").getAttribute("aria-label")).toContain("315");

        // The credit names the view's providers, after Esri's own line.
        await page.waitForFunction(() => document.getElementById("cmc-attribution")?.textContent?.includes("Vantor"));
        expect(await page.locator("#cmc-attribution").textContent()).toBe("Powered by Esri · New York State, Vantor · Leaflet");

        // Catch the export's canvas and every string drawn on it.
        await page.evaluate(() => {
            const w = window as unknown as { __export?: HTMLCanvasElement; __texts: string[] };
            w.__texts = [];
            const toBlob = HTMLCanvasElement.prototype.toBlob;
            HTMLCanvasElement.prototype.toBlob = function (this: HTMLCanvasElement, ...args: Parameters<HTMLCanvasElement["toBlob"]>) {
                w.__export = this;
                return toBlob.apply(this, args);
            };
            const fillText = CanvasRenderingContext2D.prototype.fillText;
            CanvasRenderingContext2D.prototype.fillText = function (this: CanvasRenderingContext2D, text: string, x: number, y: number, maxWidth?: number) {
                w.__texts.push(text);
                return fillText.call(this, text, x, y, maxWidth);
            };
        });
        const download = page.waitForEvent("download");
        await page.click("#cmc-download");
        expect((await download).suggestedFilename()).toBe("map-screenshot.jpg");

        const pixels = await page.evaluate(() => {
            const w = window as unknown as { __export: HTMLCanvasElement; __texts: string[] };
            const ctx = w.__export.getContext("2d")!;
            const at = (x: number, y: number): number[] => [...ctx.getImageData(x, y, 1, 1).data.slice(0, 3)];
            const { width, height } = w.__export;
            return { width, height, corners: [at(2, 2), at(width - 3, 2), at(2, height - 30)], texts: w.__texts };
        });
        // Every corner has imagery under it: the tiles cover the turned view, not just its middle.
        for (const corner of pixels.corners) expect(corner[1]).toBeGreaterThan(corner[0]! + 30);
        expect(pixels.texts.join(" ")).toContain("Powered by Esri");
        expect(pixels.texts.join(" ")).toContain("Vantor");

        // The rectangle is drawn turned: wherever the page has the rectangle under a point, so does the
        // image, and wherever it does not, neither does the image. Points near its edge are skipped.
        const agreement = await page.evaluate(() => {
            const w = window as unknown as { __export: HTMLCanvasElement };
            const ctx = w.__export.getContext("2d")!;
            const frame = document.getElementById("comment-map-composer-map")!.getBoundingClientRect();
            const onShape = (x: number, y: number): boolean => !!document.elementFromPoint(frame.left + x, frame.top + y)?.closest("[data-markup-id]");
            let checked = 0;
            let agreed = 0;
            let inside = 0;
            for (let x = 40; x < frame.width - 40; x += 23) {
                for (let y = 40; y < frame.height - 40; y += 19) {
                    const here = onShape(x, y);
                    if ([[-5, 0], [5, 0], [0, -5], [0, 5]].some(([dx, dy]) => onShape(x + dx!, y + dy!) !== here)) continue;
                    const [r, g] = ctx.getImageData(x, y, 1, 1).data;
                    const red = r! > 150 && g! < 110;
                    checked++;
                    if (here) inside++;
                    if (red === here) agreed++;
                }
            }
            return { checked, agreed, inside };
        });
        expect(agreement.inside).toBeGreaterThan(5);
        expect(agreement.agreed / agreement.checked).toBeGreaterThan(0.97);

        const body = await save(page);
        expect(body.bearing).toBe(315);
        await context.close();
    });

    test("without leaflet-rotate the composer still draws and edits, and simply does not turn", async () => {
        const { context, page, errors } = await openComposer({ query: "?norotate=1" });
        expect(await page.locator(".cmc-rotate-control").count()).toBe(0);
        const { from, to } = await drawArrow(page);
        await page.mouse.click((from.x + to.x) / 2, from.y);
        await page.keyboard.press("Backspace");
        expect(await layerNames(page)).toEqual([]);
        expect(errors).toEqual([]);
        await context.close();
    });

    test.skipIf(!SHOTS_DIR)("screenshots of the dialog in light and dark, over real tiles", async () => {
        mkdirSync(SHOTS_DIR, { recursive: true });
        for (const theme of ["light", "dark"]) {
            const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: theme as "light" | "dark" });
            const page = await context.newPage();
            await page.goto(`${server.url}?realtiles=1&theme=${theme}`);
            await page.evaluate((view) => {
                const map = { getCenter: () => ({ lat: view.lat, lng: view.lng }), getZoom: () => view.zoom };
                (window as unknown as { _openMapToolbarScreenshot: (map: unknown, context: unknown) => void })._openMapToolbarScreenshot(map, null);
            }, ALBANY_VIEW);
            await page.waitForFunction(() => !(document.getElementById("comment-map-composer-save") as HTMLButtonElement).disabled, null, { timeout: 30000 });
            // The harness strip opens on satellite, as a viewer with the default setting would.
            await page.waitForFunction(() => document.getElementById("cmc-attribution")?.textContent?.startsWith("Powered by Esri"), null, { timeout: 20000 });
            const { from, to } = await drawArrow(page);
            // The usual way round: words first, then a click to place them. Not inside the moment after
            // a drawn shape when the draw session swallows the click a drag leaves behind.
            await Bun.sleep(400);
            await page.click('.cmc-tool-btn[data-tool="text"]');
            await page.fill("#cmc-text-label", "Way in");
            await page.mouse.click(to.x + 20, to.y - 60);
            await page.click('.cmc-tool-btn[data-tool="text"]');
            await page.mouse.click((from.x + to.x) / 2, from.y);
            await page.waitForSelector(".markup-handle--rotate");
            await page.waitForLoadState("networkidle").catch(() => undefined);
            await Bun.sleep(1500);
            await page.locator("#comment-map-composer").screenshot({ path: join(SHOTS_DIR, `composer-${theme}.png`) });
            // Turned, for the rotation control and a turned label.
            for (let i = 0; i < 2; i++) await page.click('.cmc-rotate-btn[aria-label="Turn the map anticlockwise"]');
            await Bun.sleep(1500);
            await page.locator("#comment-map-composer").screenshot({ path: join(SHOTS_DIR, `composer-${theme}-turned.png`) });
            await context.close();
        }
    }, 120000);
});
