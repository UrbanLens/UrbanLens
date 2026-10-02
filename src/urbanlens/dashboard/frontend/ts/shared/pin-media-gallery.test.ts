import { beforeAll, beforeEach, describe, expect, mock, test } from "bun:test";

import { PinMediaGallery } from "./pin-media-gallery";
import { PHOTO_IDS_TYPE } from "./photo-tile";

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
        expect(sortPosts()).toEqual([["/sort/", "POST", { sort: "relevant" }, { reportsItsOwnErrors: true }]]);
        expect(document.getElementById("relevant-first")?.classList.contains("active")).toBe(true);
        expect(document.getElementById("media-gallery-section")?.classList.contains("active")).toBe(false);
    });
});

describe("PinMediaGallery dragging a tile toward the map", () => {
    function drag(tile: Element): Map<string, string> {
        const data = new Map<string, string>();
        const event = new Event("dragstart", { bubbles: true });
        Object.defineProperty(event, "dataTransfer", { value: { effectAllowed: "", setData: (type: string, value: string) => data.set(type, value) } });
        tile.dispatchEvent(event);
        return data;
    }

    test("one of your own photos travels as that photo, so dropping it moves it", () => {
        document.getElementById("media-gallery-grid")!.innerHTML = `
            <div class="media-item" data-media-source="photos" data-image-id="12" data-mine="true" draggable="true">
                <button type="button" class="media-item-thumb-btn" id="own">thumb</button>
            </div>`;
        const data = drag(document.getElementById("own")!);
        expect(data.get(PHOTO_IDS_TYPE)).toBe("[12]");
        expect(data.has("text/media-item")).toBe(false);
    });

    test("a public-source result travels as a media item to be saved where it lands", () => {
        const data = drag(document.getElementById("thumb")!.closest(".media-item")!.appendChild(document.createElement("span")));
        expect(data.has("text/media-item")).toBe(false);

        const provider = document.querySelector<HTMLElement>(".media-item")!;
        provider.setAttribute("draggable", "true");
        const sent = drag(provider);
        expect(JSON.parse(sent.get("text/media-item") ?? "{}").key).toBe("a");
        expect(sent.has(PHOTO_IDS_TYPE)).toBe(false);
    });
});
