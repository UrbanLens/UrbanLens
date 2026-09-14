import { afterEach, describe, expect, test } from "bun:test";

import { type PhotoTile, renderPhotoTile, tileFromJson, tilesForImage } from "./photo-tile";

afterEach(() => {
    document.body.innerHTML = "";
});

function photo(id: number): PhotoTile {
    const parsed = tileFromJson({ id, url: `/media/${id}.jpg` });
    if (!parsed) throw new Error("unparseable tile");
    return parsed;
}

function galleryTile(id: number): HTMLLIElement {
    const li = document.createElement("li");
    li.className = "gallery-item";
    li.id = `gallery-item-${id}`;
    li.dataset.id = String(id);
    return li;
}

function element(selector: string): HTMLElement {
    const found = document.querySelector<HTMLElement>(selector);
    if (!found) throw new Error(`missing ${selector}`);
    return found;
}

describe("album tiles beside the gallery", () => {
    test("an album tile does not take the gallery tile's id", () => {
        expect(renderPhotoTile(photo(47), { inAlbum: false }).id).toBe("");
        expect(renderPhotoTile(photo(47), { inAlbum: true, albumSlug: "interior" }).id).toBe("");
    });

    test("every copy of a photo is found, and a root narrows the search to one grid", () => {
        document.body.innerHTML = `<ul id="gallery-grid"></ul><div id="albums-panel"><ul id="albums-loose-grid"></ul></div>`;
        element("#gallery-grid").append(galleryTile(47), galleryTile(48));
        const albumCopy = renderPhotoTile(photo(47), { inAlbum: false });
        element("#albums-loose-grid").append(albumCopy);

        expect(tilesForImage(47)).toEqual([element("#gallery-item-47"), albumCopy]);
        expect(tilesForImage(47, element("#albums-panel"))).toEqual([albumCopy]);
        expect(tilesForImage(49)).toEqual([]);
    });
});
