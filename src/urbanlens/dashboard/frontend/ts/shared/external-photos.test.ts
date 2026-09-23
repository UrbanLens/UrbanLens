import { afterEach, describe, expect, test } from "bun:test";

import { externalPhotoFromJson, renderExternalPhotoTile } from "./external-photos";
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

    test("a source with no page gets no dead link", () => {
        expect(external({ page_url: "" }).querySelector("a.gallery-label-btn")).toBeNull();
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
            canRelevance: false,
            url: RAW.url,
            sourceName: "Wikimedia Commons",
            sourceUrl: RAW.page_url,
            author: "Daniel Case",
            mediaSource: "wikimedia",
            mediaKey: "abc123",
        });
    });
});
