import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { awaitRemoteCopy, displayUrl, installGlobalPhotoLightbox, samePagePath } from "./photo-lightbox";
import type { LightboxInput } from "./photo-tile";

interface Call {
    url: string;
    method: string;
    body: string;
}

const realFetch = globalThis.fetch;
const realHtmx = window.htmx;
let calls: Call[] = [];
let ajax: string[] = [];
let respond: (url: string) => Response = () => new Response("{}", { status: 200 });

beforeAll(() => {
    installGlobalPhotoLightbox();
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({ url, method: init?.method ?? "GET", body: typeof init?.body === "string" ? init.body : "" });
        return respond(url);
    }, realFetch);
    window.htmx = { process: () => undefined, trigger: () => undefined, ajax: async (_verb, url) => void ajax.push(url) };
});

afterAll(() => {
    globalThis.fetch = realFetch;
    window.htmx = realHtmx;
});

const MARKUP = `
<div class="hidden-panel" style="display:none">
<dialog id="gallery-lightbox" data-photo-lightbox
        data-cover-photo-url="/pin/p/cover/" data-current-cover-id="" data-cover-hero-key="pin" data-cover-hero-enabled="true"
        data-pin-media-relevance-url="/pin/p/relevance/" data-floorplan-overlay-url="" data-wiki-copy-url-template=""
        data-custom-fields-url="/cf/0/" data-label-image-url="/labels/00000000-0000-0000-0000-000000000000/"
        data-associations-url="/assoc/0/" data-pin-action-url="/photos/0/log-visit/" data-wiki-action-url="/photos/0/send-to-wiki/"
        data-share-action-url="/photos/0/share/" data-pin-search-url="/pin-search/" data-wiki-search-url="/wiki-search/" data-share-friends-url="/friends/">
  <button id="lightbox-prev-btn" data-lightbox-action="prev"></button>
  <img id="lightbox-img" src="">
  <span id="lightbox-fallback-note" hidden></span>
  <div id="lightbox-document-view" hidden><iframe id="lightbox-document-frame"></iframe><p id="lightbox-document-name"></p><a id="lightbox-document-open"></a></div>
  <button id="lightbox-next-btn" data-lightbox-action="next"></button>
  <p id="lightbox-caption"></p>
  <div id="lightbox-actions-menu" hidden>
    <button id="lightbox-actions-toggle" data-lightbox-action="toggle-actions"></button>
    <div id="lightbox-actions-list" hidden>
      <button id="lightbox-share-action" data-lightbox-action="share" hidden></button>
      <button id="lightbox-copy-to-pin-action" data-lightbox-action="copy-to-pin" hidden></button>
    </div>
  </div>
  <p id="lightbox-author" hidden></p><p id="lightbox-taken-at" hidden></p><p id="lightbox-copyright" hidden></p>
  <p id="lightbox-copied-from" hidden></p><p id="lightbox-source-name" hidden></p><a id="lightbox-source-link" hidden></a>
  <div id="lightbox-associations"></div>
  <div id="lightbox-location" hidden><div id="lightbox-location-map"></div><p id="lightbox-location-hint" hidden></p></div>
  <div id="lightbox-relevance-actions" hidden>
    <button id="lightbox-relevant-btn" data-lightbox-action="relevant"></button>
    <button id="lightbox-not-relevant-btn" data-lightbox-action="not-relevant"></button>
  </div>
  <div id="lightbox-cover-actions" hidden><button id="lightbox-cover-btn" data-lightbox-action="cover"></button></div>
  <div id="lightbox-custom-fields"></div><div id="lightbox-media-labels"></div>
</dialog>
</div>
<dialog id="lightbox-picker-dialog"><h3 id="lightbox-picker-title"></h3><input type="search" id="lightbox-picker-search"><div id="lightbox-picker-results"></div></dialog>
<section id="pin-detail-hero" class="ul-page-hero"></section>
<button id="hero-remove" data-cover-photo-remove></button>
<div id="media-gallery-grid"><div class="media-item" data-media-source="flickr" data-media-key="k&quot;]1"><img class="media-item-thumb" src="https://ext.test/a.jpg"></div></div>`;

function el(id: string): HTMLElement {
    const found = document.getElementById(id);
    if (!found) throw new Error(`#${id} missing`);
    return found;
}

function photos(n: number, extra: LightboxInput = {}): LightboxInput[] {
    return Array.from({ length: n }, (_, i) => ({ url: `https://x.test/${i + 1}.jpg`, caption: `photo ${i + 1}`, imageId: i + 1, ...extra }));
}

async function settle(): Promise<void> {
    for (let i = 0; i < 5; i++) await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));
}

beforeEach(() => {
    document.body.innerHTML = MARKUP;
    calls = [];
    ajax = [];
    respond = () => new Response("{}", { status: 200 });
});

describe("opening and stepping", () => {
    test("one arrow key steps one photo, wrapping at the ends", () => {
        window.galleryOpenLightboxItem?.(photos(4), 0);
        expect(el("lightbox-caption").textContent).toBe("photo 1");
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight" }));
        expect(el("lightbox-caption").textContent).toBe("photo 2");
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft" }));
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft" }));
        expect(el("lightbox-caption").textContent).toBe("photo 4");
        el("lightbox-next-btn").click();
        expect(el("lightbox-caption").textContent).toBe("photo 1");
    });

    test("the dialog leaves a hidden host for body, so showModal can paint it", () => {
        window.galleryOpenLightboxItem?.(photos(1), 0);
        expect(el("gallery-lightbox").parentElement).toBe(document.body);
        expect(el("lightbox-prev-btn").hidden).toBe(true);
    });

    test("the viewer's own photo loads its side strips; someone else's skips the labels", () => {
        window.galleryOpenLightboxItem?.([{ url: "/a.jpg", imageId: 7, uuid: "u-7" }, { url: "/b.jpg", imageId: 8, uuid: "u-8", isMine: false }], 0);
        expect(ajax).toEqual(["/cf/7/", "/labels/u-7/?embed=lightbox", "/assoc/7/"]);
        ajax = [];
        el("lightbox-next-btn").click();
        expect(ajax).toEqual(["/cf/8/", "/assoc/8/"]);
    });

    test("share is offered unless the photo is definitely someone else's; copying only when it is and the page can", () => {
        window.galleryOpenLightboxItem?.([{ url: "/a.jpg", imageId: 7 }, { url: "/b.jpg", imageId: 8, isMine: false }], 0);
        expect(el("lightbox-share-action").hidden).toBe(false);
        expect(el("lightbox-actions-menu").hidden).toBe(false);
        el("lightbox-next-btn").click();
        expect(el("lightbox-share-action").hidden).toBe(true);
        expect(el("lightbox-copy-to-pin-action").hidden).toBe(true);
        expect(el("lightbox-actions-menu").hidden).toBe(true);
    });

    test("metadata is text", () => {
        window.galleryOpenLightboxItem?.([{ url: "/a.jpg", author: "<b>x</b>", copiedFromLabel: "Ann" }], 0);
        expect(el("lightbox-author").textContent).toBe("By <b>x</b>");
        expect(el("lightbox-author").querySelector("b")).toBeNull();
        expect(el("lightbox-copied-from").textContent).toBe("Copied from Ann’s wiki");
    });
});

describe("the actions menu", () => {
    test("the toggle opens it and a click elsewhere closes it", () => {
        window.galleryOpenLightboxItem?.(photos(1), 0);
        el("lightbox-actions-toggle").click();
        expect(el("lightbox-actions-list").hidden).toBe(false);
        el("lightbox-caption").click();
        expect(el("lightbox-actions-list").hidden).toBe(true);
    });
});

describe("the cover photo", () => {
    test("setting then clearing it posts the id, remembers it, and updates the hero", async () => {
        respond = () => new Response(JSON.stringify({ cover_photo: "/media/c.jpg" }));
        window.galleryOpenLightboxItem?.(photos(1), 0);
        expect(el("lightbox-cover-actions").hidden).toBe(false);
        expect(el("lightbox-cover-btn").textContent).toContain("Set as cover photo");
        el("lightbox-cover-btn").click();
        await settle();
        expect(calls.at(-1)).toMatchObject({ url: "/pin/p/cover/", method: "POST", body: '{"image_id":1}' });
        expect(el("gallery-lightbox").dataset.currentCoverId).toBe("1");
        expect(el("lightbox-cover-btn").textContent).toContain("Remove cover photo");
        expect(el("pin-detail-hero").classList.contains("ul-page-hero--cover")).toBe(true);

        respond = () => new Response(JSON.stringify({ cover_photo: null }));
        el("hero-remove").click();
        await settle();
        expect(calls.at(-1)?.body).toBe('{"image_id":null}');
        expect(el("gallery-lightbox").dataset.currentCoverId).toBe("");
        expect(el("pin-detail-hero").classList.contains("ul-page-hero--cover")).toBe(false);
    });
});

describe("relevance", () => {
    test("marking relevant saves the local copy onto the item and its tile, found without a selector built from the key", async () => {
        respond = () => new Response(JSON.stringify({ image_url: "/media/local.jpg", image_id: 55 }));
        window.galleryOpenLightboxItem?.([{ url: "https://ext.test/a.jpg", canRelevance: true, relevant: null, mediaSource: "flickr", mediaKey: 'k"]1' }], 0);
        expect(el("lightbox-relevance-actions").hidden).toBe(false);
        el("lightbox-relevant-btn").click();
        expect(el("lightbox-relevant-btn").classList.contains("is-active")).toBe(true);
        await settle();
        expect(JSON.parse(calls.at(-1)?.body ?? "{}")).toMatchObject({ source: "flickr", item_key: 'k"]1', is_relevant: true });
        expect(el("lightbox-img").getAttribute("src")).toBe("/media/local.jpg");
        const tile = document.querySelector<HTMLElement>(".media-item");
        expect(tile?.dataset.imageId).toBe("55");
        expect(tile?.querySelector("img")?.getAttribute("src")).toBe("/media/local.jpg");
    });

    test("pressing the active choice again clears it", () => {
        window.galleryOpenLightboxItem?.([{ url: "/a.jpg", canRelevance: true, relevant: true }], 0);
        el("lightbox-relevant-btn").click();
        expect(JSON.parse(calls.at(-1)?.body ?? "{}").is_relevant).toBeNull();
    });
});

describe("the picker", () => {
    test("filing to a pin searches for the photo it opened on, and a pick files that photo even after the lightbox moves on", async () => {
        window.galleryOpenLightboxItem?.(photos(2), 0);
        document.body.insertAdjacentHTML("beforeend", '<button id="open-pin" data-lightbox-action="pin-picker"></button>');
        el("open-pin").click();
        expect(el("lightbox-picker-title").textContent).toBe("File to a pin");
        el("lightbox-next-btn").click();
        el("lightbox-picker-results").innerHTML = '<button id="pick" data-lightbox-action="pick-pin" data-pin-slug="old-mill"></button>';
        respond = () => new Response("", { status: 200, headers: { "HX-Trigger": JSON.stringify({ showToast: { message: "Filed.", level: "success" } }) } });
        el("pick").click();
        await settle();
        expect(calls.at(-1)).toMatchObject({ url: "/photos/1/log-visit/", body: "pin_slug=old-mill" });
    });

    test("the picker leaves a hidden host for body too, or it opens modal but unpainted and blocks the page", () => {
        // The wiki page includes the lightbox in its Overview panel, which is hidden while the Photos tab is open.
        el("gallery-lightbox").parentElement?.appendChild(el("lightbox-picker-dialog"));
        window.galleryOpenLightboxItem?.(photos(1), 0);
        document.body.insertAdjacentHTML("beforeend", '<button id="open-wiki" data-lightbox-action="wiki-picker"></button>');
        el("open-wiki").click();
        const picker = el("lightbox-picker-dialog");
        expect(picker instanceof HTMLDialogElement && picker.open).toBe(true);
        expect(picker.parentElement === document.body).toBe(true);
    });

    test("friends are filtered in place rather than searched", () => {
        window.galleryOpenLightboxItem?.(photos(1), 0);
        el("lightbox-share-action").click();
        expect(ajax.at(-1)).toBe("/friends/?image_id=1");
        el("lightbox-picker-results").innerHTML = '<li class="photo-share-friend-item" data-search="ann"></li><li class="photo-share-friend-item" data-search="bob"></li>';
        const search = el("lightbox-picker-search");
        if (!(search instanceof HTMLInputElement)) throw new Error("no search");
        search.value = "bo";
        search.dispatchEvent(new Event("input", { bubbles: true }));
        expect(Array.from(document.querySelectorAll<HTMLElement>(".photo-share-friend-item")).map((li) => li.hidden)).toEqual([true, false]);
    });
});

describe("helpers", () => {
    test("a format no browser renders shows the thumbnail first", () => {
        expect(displayUrl({ url: "/a.tif", thumbUrl: "/a.jpg" })).toBe("/a.jpg");
        expect(displayUrl({ url: "/a.webp?x=1", thumbUrl: "/t.jpg" })).toBe("/a.webp?x=1");
        expect(displayUrl({ url: "/a.tif" })).toBe("/a.tif");
    });

    test("this site's copy is shown in place of the provider's file", () => {
        expect(displayUrl({ url: "https://provider.test/a.jpg", thumbUrl: "/t.jpg", viewUrl: "/map/media-copy/ab/" })).toBe("/map/media-copy/ab/");
    });

    test("the lightbox image keeps an error handler after falling back, so the page's generic retry leaves it alone", () => {
        window.galleryOpenLightboxItem?.([{ url: "https://ext.test/a.jpg", viewUrl: "/map/media-copy/view/", thumbUrl: "/map/media-copy/thumb/" }], 0);
        const img = el("lightbox-img") as HTMLImageElement;
        expect(img.getAttribute("src")).toBe("/map/media-copy/view/");

        img.onerror?.(new Event("error"));

        expect(img.getAttribute("src")).toBe("/map/media-copy/thumb/");
        expect(img.onerror).not.toBeNull();
    });

    test("a copy still being made is swapped in once it exists, the thumbnail standing in meanwhile", () => {
        const timers: Array<() => void> = [];
        const realTimeout = window.setTimeout;
        window.setTimeout = ((callback: () => void) => timers.push(callback)) as typeof window.setTimeout;
        try {
            const img = document.createElement("img");
            img.classList.add("lightbox-img--fallback");
            img.src = "/thumb.jpg";
            const note = document.createElement("p");
            note.hidden = true;
            awaitRemoteCopy(img, "/map/media-copy/ab/", note);

            const probes: HTMLImageElement[] = [];
            const RealImage = window.Image;
            window.Image = class extends RealImage {
                constructor() {
                    super();
                    probes.push(this);
                }
            } as typeof window.Image;
            try {
                timers.shift()?.();
                probes[0]?.onerror?.(new Event("error"));
                timers.shift()?.();
                const probe = probes[1];
                if (!probe) throw new Error("no second probe");
                probe.onload?.(new Event("load"));
                expect(img.src).toBe(probe.src);
                expect(probe.src).toContain("/map/media-copy/ab/?_r=2");
                expect(img.classList.contains("lightbox-img--fallback")).toBe(false);
                expect(note.hidden).toBe(true);
            } finally {
                window.Image = RealImage;
            }
        } finally {
            window.setTimeout = realTimeout;
        }
    });

    test("a same-host URL is reduced to its path, another host's is left alone", () => {
        expect(samePagePath(`http://${window.location.host}/media/d.pdf?v=1#p2`)).toBe("/media/d.pdf?v=1#p2");
        expect(samePagePath("https://elsewhere.test/d.pdf")).toBe("https://elsewhere.test/d.pdf");
    });
});
