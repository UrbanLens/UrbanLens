import { beforeAll, beforeEach, describe, expect, mock, test } from "bun:test";

import { PinMediaGallery } from "./pin-media-gallery";

const sendJson = mock((..._args: unknown[]) => Promise.resolve({}));

function page(sort: string): void {
    document.body.innerHTML = `
        <div id="media-gallery-section" class="card" data-media-view="all" data-media-sort="${sort}">
            <button type="button" id="relevant-first" data-media-sort-option="relevant">Relevant first</button>
            <button type="button" id="most-recent" data-media-sort-option="recent">Most recent</button>
            <div id="media-gallery-grid">
                <div class="media-item" data-media-source="loc" data-media-key="a" data-media-url="https://example.test/a.jpg">
                    <button type="button" class="media-item-thumb-btn" id="thumb">thumb</button>
                </div>
            </div>
        </div>`;
}

beforeAll(() => {
    window.ulSendJson = sendJson as unknown as typeof window.ulSendJson;
    page("recent");
    new PinMediaGallery({ relevanceUrl: "/relevance/", sortUrl: "/sort/", sendToWikiUrl: "/wiki/" }, 0).install();
});

beforeEach(() => {
    sendJson.mockClear();
    page("recent");
});

const sortPosts = (): unknown[][] => sendJson.mock.calls.filter((call) => call[0] === "/sort/");

describe("PinMediaGallery sort options", () => {
    test("opening a tile does not save a sort preference", () => {
        document.getElementById("thumb")?.click();
        expect(sortPosts()).toEqual([]);
    });

    test("choosing a sort applies and saves it", () => {
        document.getElementById("relevant-first")?.click();
        expect(document.getElementById("media-gallery-section")?.dataset.mediaSort).toBe("relevant");
        expect(sortPosts()).toEqual([["/sort/", "POST", { sort: "relevant" }]]);
        expect(document.getElementById("relevant-first")?.classList.contains("active")).toBe(true);
        expect(document.getElementById("media-gallery-section")?.classList.contains("active")).toBe(false);
    });
});
