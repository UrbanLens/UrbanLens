import { afterEach, describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { externalPhotoFromJson, renderExternalPhotoTile } from "./external-photos";
import { displayUrl } from "./photo-lightbox";
import { lightboxListFromGrid, renderPhotoTile, tileFromJson } from "./photo-tile";

afterEach(() => {
    document.body.innerHTML = "";
});

const RAW = {
    origin: "external",
    source: "wikimedia",
    source_name: "Wikimedia Commons",
    key: "abc123",
    url: "https://upload.wikimedia.org/hrsh/1.jpg",
    thumb_url: "https://upload.wikimedia.org/hrsh/thumb/1.jpg",
    caption: "Kirkbride building",
    author: "Daniel Case",
    page_url: "https://commons.wikimedia.org/wiki/File:HRSH_1.jpg",
};

function external(overrides: Record<string, unknown> = {}): HTMLLIElement {
    const photo = externalPhotoFromJson({ ...RAW, ...overrides });
    if (!photo) throw new Error("unparseable external photo");
    return renderExternalPhotoTile(photo);
}

function own(id: number, extra: Record<string, unknown> = {}): HTMLLIElement {
    const tile = tileFromJson({ id, url: `/media/${id}.jpg`, thumb_url: `/media/${id}.thumb.jpg`, is_mine: true, ...extra });
    if (!tile) throw new Error("unparseable tile");
    return renderPhotoTile(tile, { inAlbum: false });
}

describe("external photo payload", () => {
    test("an item with nothing to show is dropped", () => {
        expect(externalPhotoFromJson({ ...RAW, thumb_url: "" })).toBeNull();
        expect(externalPhotoFromJson({ ...RAW, key: "" })).toBeNull();
    });
});

describe("external photo tile", () => {
    test("names its source and links to it", () => {
        const tile = external();

        expect(tile.dataset.mediaKey).toBe("abc123");
        expect(tile.dataset.id).toBeUndefined();
        expect(tile.querySelector(".gallery-child-ribbon")?.textContent).toBe("Wikimedia Commons");
        expect(tile.querySelector<HTMLAnchorElement>("a.gallery-label-btn")?.href).toBe(RAW.page_url);
        expect(tile.querySelector<HTMLImageElement>("img.gallery-thumb")?.getAttribute("src")).toBe(RAW.thumb_url);
    });

    test("provider text is never parsed as markup", () => {
        const tile = external({ caption: '<img src=x onerror="alert(1)">', source_name: "<b>Evil</b>" });

        expect(tile.querySelectorAll("img").length).toBe(1);
        expect(tile.querySelector("b")).toBeNull();
        expect(tile.querySelector(".album-item-caption")?.textContent).toBe('<img src=x onerror="alert(1)">');
    });

    test("a provider link that is not a web address is dropped", () => {
        const tile = external({ page_url: "javascript:alert(1)" });

        expect(tile.querySelector("a.gallery-label-btn")).toBeNull();
        expect(tile.dataset.sourceUrl).toBe("");
    });

    test("a source with no page gets no dead link", () => {
        expect(external({ page_url: "" }).querySelector("a.gallery-label-btn")).toBeNull();
    });
});

describe("the lightbox shows this site's copy of a public photo", () => {
    test("its copy is the lightbox's picture, and the provider's address stays its identity", () => {
        document.body.innerHTML = `<div data-lightbox-scope><ul id="public"></ul></div>`;
        const tile = external({ view_url: "/dashboard/map/media-copy/abc/" });
        document.getElementById("public")!.append(tile);

        const { list } = lightboxListFromGrid(document.querySelector<HTMLElement>("[data-lightbox-scope]")!, tile.querySelector("button")!);

        expect(list[0]).toMatchObject({ url: RAW.url, viewUrl: "/dashboard/map/media-copy/abc/" });
        expect(displayUrl(list[0]!)).toBe("/dashboard/map/media-copy/abc/");
    });

    test("only a same-site address is taken as the copy", () => {
        for (const viewUrl of ["https://elsewhere.example/x.jpg", "//elsewhere.example/x.jpg", "javascript:alert(1)"]) {
            expect(externalPhotoFromJson({ ...RAW, view_url: viewUrl })?.viewUrl).toBe("");
        }
    });
});

describe("one lightbox across your photos and public ones", () => {
    test("both kinds are in the list, in page order, attributed", () => {
        document.body.innerHTML = `<div data-lightbox-scope><ul id="mine"></ul><ul id="public"></ul></div>`;
        const scope = document.querySelector<HTMLElement>("[data-lightbox-scope]")!;
        document.getElementById("mine")!.append(own(1), own(2, { processing: true }));
        const publicTile = external();
        document.getElementById("public")!.append(publicTile);

        const { list, idx } = lightboxListFromGrid(scope, publicTile.querySelector("button")!);

        expect(list.length).toBe(2);
        expect(idx).toBe(1);
        expect(list[0]?.imageId).toBe(1);
        expect(list[1]).toMatchObject({
            imageId: null,
            isMine: false,
            canRelevance: true,
            relevant: null,
            url: RAW.url,
            sourceName: "Wikimedia Commons",
            sourceUrl: RAW.page_url,
            author: "Daniel Case",
            mediaSource: "wikimedia",
            mediaKey: "abc123",
        });
    });
});

describe("a public photo's relevance", () => {
    test("the viewer's mark travels from the payload into the lightbox, which offers the votes", () => {
        document.body.innerHTML = `<div data-lightbox-scope><ul id="public"></ul></div>`;
        const grid = document.getElementById("public")!;
        grid.append(external({ key: "up", relevant: true }), external({ key: "down", relevant: false }), external({ key: "none" }));

        const { list } = lightboxListFromGrid(document.querySelector<HTMLElement>("[data-lightbox-scope]")!, grid.querySelector("button")!);

        expect(list.map((item) => [item.mediaKey, item.canRelevance, item.relevant])).toEqual([
            ["up", true, true],
            ["down", true, false],
            ["none", true, null],
        ]);
    });
});

describe("a public tile whose copy is still being made", () => {
    test("asks again once per failure, then shows an icon in the thumbnail's place", () => {
        const timers: Array<() => void> = [];
        const script = readFileSync(join(import.meta.dir, "..", "..", "static", "js", "media-thumb-fallback.js"), "utf8");
        new Function("window", "setTimeout", script)(window, (callback: () => void) => timers.push(callback));
        document.body.innerHTML = `<ul id="public"></ul>`;
        const tile = external({ thumb_url: "/dashboard/map/media-copy/abc/" });
        document.getElementById("public")!.append(tile);

        tile.querySelector("img")!.dispatchEvent(new Event("error"));
        expect(timers).toHaveLength(1);

        for (let attempt = 0; attempt < 20 && tile.querySelector("img"); attempt++) {
            timers.splice(0).forEach((timer) => timer());
            tile.querySelector("img")?.dispatchEvent(new Event("error"));
        }
        expect(tile.querySelector("img")).toBeNull();
        expect(tile.querySelector(".gallery-thumb-btn > .gallery-thumb.gallery-thumb--placeholder")?.textContent).toBe("broken_image");
    });
});
