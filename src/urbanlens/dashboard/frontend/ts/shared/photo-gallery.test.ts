import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import type { LightboxInput } from "./photo-tile";
import { installPhotoGallery, setPhotoMapHidden } from "./photo-gallery";

interface Call {
    url: string;
    method: string;
    body: BodyInit | null | undefined;
}

const realFetch = globalThis.fetch;
const realConfirm = window.confirm;
const realToastr = window.toastr;
let calls: Call[] = [];
let respond: (call: Call) => Response = () => new Response(null, { status: 204 });
let toasts: { success: string[]; error: string[] };
let confirms: string[] = [];
let answers: boolean[] = [];
let markers: { added: number[]; removed: number[] };
let opened: { list: LightboxInput[]; idx: number } | null = null;
let bulkActions: Record<string, (() => void) | null | undefined> = {};

function tile(id: number, overrides: Record<string, string> = {}): string {
    const data: Record<string, string> = {
        id: String(id), uuid: `u-${id}`, url: `/m/${id}.jpg`, "thumb-url": `/t/${id}.jpg`, lat: "", lng: "",
        mine: "true", "on-wiki": "false", uploaded: "true", "on-pin": "true", "map-hidden": "false", ...overrides,
    };
    const attrs = Object.entries(data).map(([k, v]) => `data-${k}="${v}"`).join(" ");
    return `<li class="gallery-item" id="gallery-item-${id}" ${attrs}>
      <button type="button" class="gallery-select-check" data-gallery-action="select" hidden></button>
      <button type="button" class="gallery-thumb-btn" data-gallery-action="open"><img class="gallery-thumb"></button>
      <button type="button" class="gallery-delete-btn" data-gallery-action="delete"></button>
    </li>`;
}

function render(context = "pin", tiles = [tile(1), tile(2), tile(3)]): void {
    document.body.innerHTML = `
    <div id="pin-gallery-panel">
      <div class="photo-gallery card" id="photo-gallery" data-processing-url="/processing/" data-gallery-context="${context}"
           data-gallery-url="/pin/p/gallery/" data-gallery-bulk-url="/pin/p/gallery/bulk/">
        <div class="card-header"><i></i><span>Photos</span><span class="badge">${tiles.length}</span></div>
        <input type="file" id="gallery-file-input" multiple hidden>
        <ul class="gallery-grid" id="gallery-grid">${tiles.join("")}${tiles.length ? `<li class="gallery-add-item" id="gallery-add-btn"></li>` : `<li class="gallery-empty" id="gallery-empty"></li>`}</ul>
      </div>
      <div id="gallery-drop-overlay" hidden></div>
    </div>`;
    document.dispatchEvent(new Event("htmx:load"));
}

function click(selector: string): void {
    const el = document.querySelector<HTMLElement>(selector);
    if (!el) throw new Error(`no ${selector}`);
    el.click();
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const ids = () => Array.from(document.querySelectorAll<HTMLElement>("#gallery-grid .gallery-item"), (el) => Number(el.dataset.id));
const badge = () => document.querySelector("#photo-gallery .badge")?.textContent ?? null;

beforeAll(() => {
    installPhotoGallery();
});

afterAll(() => {
    window.toastr = realToastr;
    globalThis.fetch = realFetch;
    window.confirm = realConfirm;
});

beforeEach(() => {
    calls = [];
    confirms = [];
    answers = [];
    opened = null;
    bulkActions = {};
    respond = () => new Response(null, { status: 204 });
    toasts = { success: [], error: [] };
    markers = { added: [], removed: [] };
    window.csrftoken = "tok";
    window.toastr = {
        success: (m) => void toasts.success.push(m),
        error: (m) => void toasts.error.push(m),
        warning: () => undefined,
        info: () => undefined,
        clear: () => undefined,
    };
    window.confirm = (message?: string) => {
        confirms.push(message ?? "");
        return answers.shift() ?? true;
    };
    window._galleryAddMarker = (img) => void markers.added.push(img.id);
    window._galleryRemoveMarker = (id) => void markers.removed.push(id);
    window.galleryOpenLightboxItem = (list, idx) => {
        opened = { list, idx };
    };
    window.ulBulkToolbar = {
        sync: (_ns, _count, actions) => {
            bulkActions = actions;
        },
        clear: () => undefined,
    };
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL, init?: RequestInit) => {
        const call = { url: String(input), method: init?.method ?? "GET", body: init?.body };
        calls.push(call);
        return respond(call);
    }, realFetch);
    render();
});

describe("bulk delete", () => {
    function selectAll(): void {
        click('[data-gallery-action="select"]');
        document.querySelectorAll<HTMLElement>('#gallery-item-2 [data-gallery-action="select"]').forEach((b) => b.click());
    }

    test("a refused delete keeps the photos and says so", async () => {
        respond = () => new Response(JSON.stringify({ error: "Too many photos." }), { status: 400 });
        selectAll();
        bulkActions.delete?.();
        await settle();

        expect(ids()).toEqual([1, 2, 3]);
        expect(toasts.success).toEqual([]);
        expect(toasts.error).toEqual(["Too many photos."]);
        expect(badge()).toBe("3");
    });

    test("a delete takes the tiles, their markers and the count, and reports what stayed on the wiki", async () => {
        respond = () => new Response(JSON.stringify({ deleted: 1, unlinked: 1, image_ids: [1, 2] }), { status: 200 });
        selectAll();
        bulkActions.delete?.();
        await settle();

        expect(JSON.parse(String(calls[0]?.body))).toEqual({ action: "delete", image_ids: [1, 2] });
        expect(ids()).toEqual([3]);
        expect(markers.removed).toEqual([1, 2]);
        expect(badge()).toBe("1");
        expect(toasts.success).toEqual(["Deleted 1 photo. Removed 1 photo from this pin; still on the wiki."]);
        expect(document.getElementById("photo-gallery")?.classList.contains("photo-gallery--selecting")).toBe(false);
    });

    test("only the photos the server acted on leave the grid", async () => {
        respond = () => new Response(JSON.stringify({ deleted: 1, unlinked: 0, image_ids: [1] }), { status: 200 });
        selectAll();
        bulkActions.delete?.();
        await settle();

        expect(ids()).toEqual([2, 3]);
        expect(markers.removed).toEqual([1]);
        expect(badge()).toBe("2");
    });

    test("declining the prompt sends nothing", async () => {
        answers = [false];
        selectAll();
        bulkActions.delete?.();
        await settle();
        expect(calls).toEqual([]);
        expect(ids()).toEqual([1, 2, 3]);
    });

    test("selecting mode turns a thumbnail click into a selection", () => {
        click('#gallery-item-1 [data-gallery-action="select"]');
        click('#gallery-item-2 [data-gallery-action="open"]');
        expect(opened).toBeNull();
        expect(document.getElementById("gallery-item-2")?.classList.contains("is-selected")).toBe(true);
    });
});

describe("single delete", () => {
    test("a photo only on the pin is deleted outright", async () => {
        click('#gallery-item-2 [data-gallery-action="delete"]');
        await settle();
        expect(calls.map((c) => `${c.method} ${c.url}`)).toEqual(["DELETE /pin/p/gallery/2/"]);
        expect(ids()).toEqual([1, 3]);
        expect(badge()).toBe("2");
        expect(toasts.success).toEqual(["Photo deleted."]);
    });

    test("a contribution stays on the wiki unless withdrawing it is asked for", async () => {
        render("pin", [tile(1, { "on-wiki": "true" })]);
        answers = [true, false];
        click('#gallery-item-1 [data-gallery-action="delete"]');
        await settle();
        expect(confirms).toHaveLength(2);
        expect(calls[0]?.url).toBe("/pin/p/gallery/1/");
        expect(toasts.success).toEqual(["Removed from this pin. Still on the wiki."]);
    });

    test("withdrawing a contribution says so to the server", async () => {
        render("pin", [tile(1, { "on-wiki": "true" })]);
        answers = [true, true];
        click('#gallery-item-1 [data-gallery-action="delete"]');
        await settle();
        expect(calls[0]?.url).toBe("/pin/p/gallery/1/?from_wiki=1");
    });

    test("a failed delete keeps the tile and says so", async () => {
        respond = () => new Response("", { status: 500 });
        click('#gallery-item-1 [data-gallery-action="delete"]');
        await settle();
        expect(ids()).toEqual([1, 2, 3]);
        expect(toasts.error).toEqual(["Could not delete that photo."]);
    });

    test("the wiki gallery only takes a photo that is still on a pin off the wiki", async () => {
        render("wiki");
        click('#gallery-item-1 [data-gallery-action="delete"]');
        await settle();
        expect(confirms[0]).toContain("It stays on your pin.");
        expect(toasts.success).toEqual(["Removed from the wiki. Still on your pin."]);
    });
});

describe("upload", () => {
    test("an uploaded photo joins the grid ready to open, select and delete", async () => {
        render("pin", []);
        respond = () => new Response(JSON.stringify({ id: 9, uuid: "u-9", url: "/m/9.jpg", thumb_url: "/t/9.jpg", latitude: 1, longitude: 2, is_mine: true, uploaded: true }), { status: 201 });
        const input = document.getElementById("gallery-file-input");
        if (!(input instanceof HTMLInputElement)) throw new Error("no input");
        Object.defineProperty(input, "files", { value: [new File(["x"], "a.jpg")], configurable: true });
        input.dispatchEvent(new Event("change", { bubbles: true }));
        await settle();

        expect(calls[0]?.url).toBe("/pin/p/gallery/");
        expect(ids()).toEqual([9]);
        expect(document.getElementById("gallery-empty")).toBeNull();
        expect(document.getElementById("gallery-add-btn")).not.toBeNull();
        expect(badge()).toBe("1");
        expect(markers.added).toEqual([9]);
        const actions = Array.from(document.querySelectorAll<HTMLElement>("#gallery-item-9 [data-gallery-action]"), (b) => b.dataset.galleryAction);
        expect(actions).toEqual(["select", "open", "delete"]);
        expect(document.querySelector("#gallery-item-9 .gallery-has-coords")).not.toBeNull();
    });

    test("a rejected upload leaves the grid alone and reports the server's reason", async () => {
        respond = () => new Response(JSON.stringify({ error: "You already have this photo." }), { status: 409 });
        const input = document.getElementById("gallery-file-input");
        if (!(input instanceof HTMLInputElement)) throw new Error("no input");
        Object.defineProperty(input, "files", { value: [new File(["x"], "a.jpg")], configurable: true });
        input.dispatchEvent(new Event("change", { bubbles: true }));
        await settle();

        expect(ids()).toEqual([1, 2, 3]);
        expect(toasts.error).toEqual(["Upload failed: You already have this photo."]);
    });
});

describe("lightbox", () => {
    test("opens on the clicked photo, skipping ones with no file", () => {
        render("pin", [tile(1), tile(2, { processing: "failed" }), tile(3)]);
        click('#gallery-item-3 [data-gallery-action="open"]');
        expect(opened?.list.map((i) => i.imageId)).toEqual([1, 3]);
        expect(opened?.idx).toBe(1);
    });

    test("a photo off this page opens alone from the map's fallback", () => {
        window.galleryOpenLightbox?.(42, { url: "/m/42.jpg" });
        expect(opened?.list.map((i) => i.url)).toEqual(["/m/42.jpg"]);
    });

    test("reads the tiles as they are now, not as the page loaded", async () => {
        respond = () => new Response(JSON.stringify({ latitude: "1.5", longitude: "2.5", map_hidden: false }), { status: 200 });
        window.galleryRepositionImage?.(1, 1.5, 2.5);
        await settle();
        click('#gallery-item-1 [data-gallery-action="open"]');
        expect(opened?.list[0]?.latitude).toBe(1.5);
        expect(document.querySelector("#gallery-item-1 .gallery-has-coords")).not.toBeNull();
    });
});

describe("map visibility", () => {
    test("hiding drops the marker; showing puts it back", async () => {
        render("pin", [tile(1, { lat: "1", lng: "2" })]);
        respond = () => new Response(JSON.stringify({ map_hidden: true }), { status: 200 });
        await setPhotoMapHidden(1, true);
        expect(markers.removed).toEqual([1]);
        expect(document.getElementById("gallery-item-1")?.dataset.mapHidden).toBe("true");

        respond = () => new Response(JSON.stringify({ map_hidden: false }), { status: 200 });
        await setPhotoMapHidden(1, false);
        expect(markers.added).toEqual([1]);
    });

    test("a refused change calls the caller back so the marker can snap back", async () => {
        respond = () => new Response("", { status: 403 });
        let rejected = false;
        window.galleryRepositionImage?.(1, 1, 2, () => {
            rejected = true;
        });
        await settle();
        expect(rejected).toBe(true);
    });
});

describe("drop to upload", () => {
    function drag(type: string, target: EventTarget, files: File[] = []): void {
        const event = new Event(type, { bubbles: true, cancelable: true });
        Object.defineProperty(event, "dataTransfer", { value: { types: ["Files"], files } });
        target.dispatchEvent(event);
    }

    test("the overlay moves to the body so a hidden tab cannot hide it", () => {
        expect(document.getElementById("gallery-drop-overlay")?.parentElement).toBe(document.body);
    });

    test("a file dragged in from outside shows the overlay, and dropping it there uploads", async () => {
        const overlay = document.getElementById("gallery-drop-overlay");
        if (!overlay) throw new Error("no overlay");
        drag("dragenter", document.body);
        expect(overlay.hidden).toBe(false);
        drag("drop", overlay, [new File(["x"], "a.jpg")]);
        await settle();
        expect(overlay.hidden).toBe(true);
        expect(calls.map((c) => c.url)).toEqual(["/pin/p/gallery/"]);
    });

    test("dragging a photo already on the page never uploads it", async () => {
        const overlay = document.getElementById("gallery-drop-overlay");
        if (!overlay) throw new Error("no overlay");
        document.querySelector("#gallery-item-1 img")?.dispatchEvent(new Event("dragstart", { bubbles: true }));
        drag("dragenter", document.body);
        expect(overlay.hidden).toBe(true);
        drag("drop", overlay, [new File(["x"], "a.jpg")]);
        await settle();
        expect(calls).toEqual([]);
    });
});
