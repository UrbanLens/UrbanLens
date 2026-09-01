/**
 * media-lightbox.ts fixed a real bug (docs/PROBLEMS.md, 2026-09-01): the wiki
 * page's Media section had no `window.mediaOpenLightbox`, so clicking a tile
 * threw. These tests exercise the DOM-parsing logic that broke silently
 * before - nothing here previously had any coverage at all.
 */

import { beforeEach, describe, expect, test } from "bun:test";

import { openMediaLightbox } from "./media-lightbox";
import type { LightboxItem } from "./photo-tile";

let calls: Array<{ list: LightboxItem[]; idx: number }> = [];

beforeEach(() => {
    document.body.innerHTML = "";
    calls = [];
    window.galleryOpenLightboxItem = (list, idx) => {
        calls.push({ list, idx });
    };
});

/** Build a `.media-item` tile with the given data attributes, matching pin_media_items.html. */
function buildTile(attrs: Record<string, string>, opts: { excluded?: boolean } = {}): HTMLElement {
    const el = document.createElement("div");
    el.className = opts.excluded ? "media-item media-tab-excluded" : "media-item";
    for (const [key, value] of Object.entries(attrs)) el.dataset[key] = value;
    el.innerHTML = `<button type="button" class="media-item-thumb-btn">thumb</button>`;
    return el;
}

function buildGrid(id: string, relevanceUrl: string | null, tiles: HTMLElement[]): HTMLElement {
    const grid = document.createElement("div");
    grid.className = "media-gallery-grid";
    grid.id = id;
    if (relevanceUrl) grid.dataset.relevanceUrl = relevanceUrl;
    tiles.forEach((tile) => grid.appendChild(tile));
    document.body.appendChild(grid);
    return grid;
}

describe("openMediaLightbox", () => {
    test("does nothing when the button isn't inside a .media-item", () => {
        const button = document.createElement("button");
        document.body.appendChild(button);
        openMediaLightbox(button);
        expect(calls).toEqual([]);
    });

    test("does nothing when the .media-item isn't inside a .media-gallery-grid", () => {
        const tile = buildTile({ mediaUrl: "https://example.com/a.jpg" });
        document.body.appendChild(tile);
        openMediaLightbox(tile.querySelector("button") as HTMLElement);
        expect(calls).toEqual([]);
    });

    test("finds the shared grid by class, regardless of the page's own id", () => {
        const tile = buildTile({ mediaUrl: "https://example.com/a.jpg" });
        buildGrid("wiki-media-grid", null, [tile]);
        openMediaLightbox(tile.querySelector("button") as HTMLElement);
        expect(calls).toHaveLength(1);
    });

    test("maps every data attribute onto the LightboxItem shape", () => {
        const tile = buildTile({
            mediaSource: "flickr",
            mediaSourceName: "Flickr",
            mediaKey: "flickr:123",
            mediaUrl: "https://example.com/full.jpg",
            mediaThumb: "https://example.com/thumb.jpg",
            mediaPageUrl: "https://example.com/page",
            mediaCaption: "A caption",
            imageId: "42",
            mine: "true",
            mediaRelevant: "true",
            lat: "40.5",
            lng: "-73.9",
            copiedFromLabel: "Jane's wiki",
            mediaAuthor: "Jane Doe",
        });
        buildGrid("media-gallery-grid", "/pin/abc/media/relevance", [tile]);
        openMediaLightbox(tile.querySelector("button") as HTMLElement);

        expect(calls).toHaveLength(1);
        expect(calls[0]?.idx).toBe(0);
        expect(calls[0]?.list).toEqual([
            {
                url: "https://example.com/full.jpg",
                thumbUrl: "https://example.com/thumb.jpg",
                caption: "A caption",
                author: "Jane Doe",
                copyright: "",
                sourceUrl: "https://example.com/page",
                sourceName: "Flickr",
                takenAt: "",
                imageId: 42,
                uuid: "",
                isMine: true,
                canRelevance: true,
                relevant: true,
                latitude: 40.5,
                longitude: -73.9,
                mapHidden: false,
                copiedFromLabel: "Jane's wiki",
                mediaSource: "flickr",
                mediaKey: "flickr:123",
            },
        ]);
    });

    test("falls back from the page url to the full-size url when there's no page url", () => {
        const tile = buildTile({ mediaUrl: "https://example.com/full.jpg" });
        buildGrid("media-gallery-grid", null, [tile]);
        openMediaLightbox(tile.querySelector("button") as HTMLElement);
        expect(calls[0]?.list[0]?.sourceUrl).toBe("https://example.com/full.jpg");
    });

    test("author is read from data-media-author, not hardcoded blank", () => {
        // Regression: a copied photo's "By {author}" line silently never
        // rendered on this path because this function always hardcoded "" -
        // see docs/PROBLEMS.md, 2026-09-01.
        const credited = buildTile({ mediaUrl: "a", mediaAuthor: "Uploaded by john" });
        const uncredited = buildTile({ mediaUrl: "b" });
        buildGrid("media-gallery-grid", null, [credited, uncredited]);

        openMediaLightbox(credited.querySelector("button") as HTMLElement);
        expect(calls[0]?.list[0]?.author).toBe("Uploaded by john");

        openMediaLightbox(uncredited.querySelector("button") as HTMLElement);
        expect(calls[1]?.list[1]?.author).toBe("");
    });

    test("an absent image id parses as null, not zero or NaN", () => {
        const tile = buildTile({ mediaUrl: "https://example.com/a.jpg" });
        buildGrid("media-gallery-grid", null, [tile]);
        openMediaLightbox(tile.querySelector("button") as HTMLElement);
        expect(calls[0]?.list[0]?.imageId).toBeNull();
    });

    test("an external-provider item with no ownership data defaults isMine to true", () => {
        // Matches _setLightboxMeta's own "isMine !== false" convention - an
        // external search result was never a candidate for "not mine" before.
        const tile = buildTile({ mediaUrl: "https://example.com/a.jpg" });
        buildGrid("media-gallery-grid", null, [tile]);
        openMediaLightbox(tile.querySelector("button") as HTMLElement);
        expect(calls[0]?.list[0]?.isMine).toBe(true);
    });

    test("an explicit data-mine=false is honored, unlike an absent attribute", () => {
        const tile = buildTile({ mediaUrl: "https://example.com/a.jpg", mine: "false" });
        buildGrid("media-gallery-grid", null, [tile]);
        openMediaLightbox(tile.querySelector("button") as HTMLElement);
        expect(calls[0]?.list[0]?.isMine).toBe(false);
    });

    test("relevant is a real tri-state: true, false, and absent (null)", () => {
        const yes = buildTile({ mediaUrl: "a", mediaKey: "yes", mediaRelevant: "true" });
        const no = buildTile({ mediaUrl: "b", mediaKey: "no", mediaRelevant: "false" });
        const unset = buildTile({ mediaUrl: "c", mediaKey: "unset" });
        buildGrid("media-gallery-grid", null, [yes, no, unset]);

        openMediaLightbox(yes.querySelector("button") as HTMLElement);
        openMediaLightbox(no.querySelector("button") as HTMLElement);
        openMediaLightbox(unset.querySelector("button") as HTMLElement);

        expect(calls[0]?.list.map((item) => item.relevant)).toEqual([true, false, null]);
        expect(calls[1]?.list.map((item) => item.relevant)).toEqual([true, false, null]);
        expect(calls[2]?.list.map((item) => item.relevant)).toEqual([true, false, null]);
    });

    test("invalid lat/lng strings parse as null rather than NaN", () => {
        const tile = buildTile({ mediaUrl: "a", lat: "not-a-number", lng: "" });
        buildGrid("media-gallery-grid", null, [tile]);
        openMediaLightbox(tile.querySelector("button") as HTMLElement);
        expect(calls[0]?.list[0]?.latitude).toBeNull();
        expect(calls[0]?.list[0]?.longitude).toBeNull();
    });

    test("canRelevance requires both a relevance url on the grid and a non-photos source", () => {
        const ownPhoto = buildTile({ mediaUrl: "a", mediaSource: "photos" });
        const external = buildTile({ mediaUrl: "b", mediaSource: "flickr" });
        buildGrid("media-gallery-grid", "/relevance", [ownPhoto, external]);

        openMediaLightbox(ownPhoto.querySelector("button") as HTMLElement);
        expect(calls[0]?.list[0]?.canRelevance).toBe(false);

        openMediaLightbox(external.querySelector("button") as HTMLElement);
        expect(calls[1]?.list[1]?.canRelevance).toBe(true);
    });

    test("canRelevance is false on a grid with no relevance url at all (the wiki page)", () => {
        const tile = buildTile({ mediaUrl: "a", mediaSource: "flickr" });
        buildGrid("wiki-media-grid", null, [tile]);
        openMediaLightbox(tile.querySelector("button") as HTMLElement);
        expect(calls[0]?.list[0]?.canRelevance).toBe(false);
    });

    test("excludes tab-filtered tiles and re-indexes the clicked item against what's left", () => {
        const hiddenTile = buildTile({ mediaUrl: "hidden", mediaKey: "h" }, { excluded: true });
        const first = buildTile({ mediaUrl: "first", mediaKey: "f" });
        const clicked = buildTile({ mediaUrl: "clicked", mediaKey: "c" });
        buildGrid("media-gallery-grid", null, [hiddenTile, first, clicked]);

        openMediaLightbox(clicked.querySelector("button") as HTMLElement);

        expect(calls[0]?.list.map((item) => item.mediaKey)).toEqual(["f", "c"]);
        expect(calls[0]?.idx).toBe(1);
    });
});
