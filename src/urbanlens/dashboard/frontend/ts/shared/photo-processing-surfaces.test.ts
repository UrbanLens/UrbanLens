import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { observeMediaGalleryProcessing, openMediaLightbox, settleMediaItem } from "./media-lightbox";
import { FAILED_LABEL, installGlobalPhotoProcessing, PROCESSING_LABEL, processingPlaceholder, settleProcessingThumb } from "./photo-processing";
import type { LightboxItem } from "./photo-tile";
import { renderVaultDocumentTile, settleVaultDocumentTile } from "./vault-document-grid";

const PENDING = { id: 7, url: "", thumb_url: "", caption: "Boiler room", processing: true, processing_failed: false };
const FAILED = { ...PENDING, processing: false, processing_failed: true };
const READY = { ...PENDING, processing: false, url: "https://m.test/media/pin_images/a/p.webp", thumb_url: "https://m.test/media/pin_images/t/p.webp" };
const DOC_PENDING = { ...PENDING, caption: "deed.txt", document_icon: "article" };
const DOC_READY = { ...DOC_PENDING, processing: false, url: "https://m.test/media/pin_images/a/deed.pdf", document_icon: "picture_as_pdf" };

afterEach(() => {
    document.body.innerHTML = "";
    window.urbanlensProcessingPollers?.forEach((poller) => poller.stop());
    window.urbanlensProcessingPollers?.clear();
});

/** A server-rendered placeholder tile, shaped like `_processing_thumb.html` inside a disabled open button. */
function pendingTile(baseClass: string): HTMLElement {
    const li = document.createElement("li");
    li.dataset.id = "7";
    li.dataset.processing = "pending";
    const button = document.createElement("button");
    button.disabled = true;
    button.dataset.processingOpen = "";
    button.append(processingPlaceholder(baseClass));
    li.append(button);
    document.body.append(li);
    return li;
}

describe("vault document tile", () => {
    test("a processing document renders a placeholder and no file", () => {
        const li = renderVaultDocumentTile(DOC_PENDING);
        expect(li).not.toBeNull();
        expect(li?.dataset.processing).toBe("pending");
        expect(li?.dataset.url).toBe("");
        expect(li?.querySelector<HTMLButtonElement>(".document-tile-btn")?.disabled).toBe(true);
        expect(li?.querySelector(".document-tile-icon")?.getAttribute("aria-label")).toBe(PROCESSING_LABEL);
        expect(li?.querySelector(".document-tile-name")?.textContent).toBe("deed.txt");
    });

    test("a failed document renders the failed placeholder", () => {
        const li = renderVaultDocumentTile({ ...DOC_PENDING, processing: false, processing_failed: true });
        expect(li?.dataset.processing).toBe("failed");
        expect(li?.querySelector(".document-tile-icon")?.getAttribute("aria-label")).toBe(FAILED_LABEL);
    });

    test("a document with neither a file nor a processing state is skipped", () => {
        expect(renderVaultDocumentTile({ id: 7, url: "" })).toBeNull();
    });

    test("settling swaps in the ready tile, and drops one that is gone", () => {
        const pending = renderVaultDocumentTile(DOC_PENDING);
        if (!pending) throw new Error("unrendered");
        document.body.append(pending);
        settleVaultDocumentTile(pending, DOC_READY);
        const ready = document.getElementById("document-tile-7");
        expect(ready?.hasAttribute("data-processing")).toBe(false);
        expect(ready?.dataset.url).toBe(DOC_READY.url);
        expect(ready?.querySelector(".document-tile-icon")?.textContent).toBe("picture_as_pdf");
        if (!ready) throw new Error("not swapped");
        settleVaultDocumentTile(ready, null);
        expect(document.getElementById("document-tile-7")).toBeNull();
    });
});

describe("settling a server-rendered placeholder", () => {
    test("a ready photo gets its thumbnail and an enabled open button", () => {
        const li = pendingTile("photo-tile-fallback");
        settleProcessingThumb(li, READY, "gallery-thumb");
        expect(li.hasAttribute("data-processing")).toBe(false);
        expect(li.dataset.url).toBe(READY.url);
        const img = li.querySelector("img");
        expect(img?.getAttribute("src")).toBe(READY.thumb_url);
        expect(img?.className).toBe("gallery-thumb");
        expect(li.querySelector(".media-processing")).toBeNull();
        expect(li.querySelector("button")?.disabled).toBe(false);
    });

    test("a failed photo keeps a failed placeholder and a disabled button", () => {
        const li = pendingTile("photo-tile-fallback");
        settleProcessingThumb(li, FAILED);
        expect(li.dataset.processing).toBe("failed");
        const placeholder = li.querySelector('[role="img"]');
        expect(placeholder?.getAttribute("aria-label")).toBe(FAILED_LABEL);
        expect(placeholder?.classList.contains("photo-tile-fallback")).toBe(true);
        expect(li.querySelector("img")).toBeNull();
        expect(li.querySelector("button")?.disabled).toBe(true);
    });

    test("a photo that is gone leaves the page", () => {
        const li = pendingTile("photo-tile-fallback");
        settleProcessingThumb(li, null);
        expect(li.isConnected).toBe(false);
    });

    test("a tile that is its own open button shows the full file and is enabled", () => {
        const button = document.createElement("button");
        button.disabled = true;
        button.dataset.id = "7";
        button.dataset.processing = "pending";
        button.dataset.processingOpen = "";
        button.append(processingPlaceholder("dm-bubble__image"));
        document.body.append(button);

        settleProcessingThumb(button, READY, "dm-bubble__image", "full");

        expect(button.querySelector("img")?.getAttribute("src")).toBe(READY.url);
        expect(button.disabled).toBe(false);
    });

    test("a failed tile that is its own open button stays disabled", () => {
        const button = document.createElement("button");
        button.disabled = true;
        button.dataset.processingOpen = "";
        button.append(processingPlaceholder("dm-bubble__image"));
        document.body.append(button);

        settleProcessingThumb(button, FAILED, "dm-bubble__image", "full");

        expect(button.disabled).toBe(true);
        expect(button.querySelector("img")).toBeNull();
    });
});

describe("watching one photo from an inline script", () => {
    test("shares the page's poller for the status URL", () => {
        installGlobalPhotoProcessing();
        const el = document.body.appendChild(document.createElement("span"));

        window.urbanlensWatchProcessing?.("/vault/photos/processing/", 7, el, () => undefined);

        expect(window.urbanlensProcessingPollers?.get("/vault/photos/processing/")?.size).toBe(1);
    });
});

describe("media gallery My Photos tile", () => {
    let calls: Array<{ list: LightboxItem[]; idx: number }> = [];

    beforeEach(() => {
        calls = [];
        window.galleryOpenLightboxItem = (list, idx) => {
            calls.push({ list, idx });
        };
    });

    function mediaTile(key: string, processing: boolean): HTMLElement {
        const el = document.createElement("div");
        el.className = "media-item";
        el.dataset.mediaSource = "photos";
        el.dataset.mediaKey = key;
        el.dataset.mediaUrl = processing ? "" : `https://m.test/${key}.webp`;
        const button = document.createElement("button");
        button.className = "media-item-thumb-btn";
        if (processing) {
            el.dataset.id = "7";
            el.dataset.processing = "pending";
            el.dataset.processingUrl = "/vault/photos/processing/";
            button.disabled = true;
            button.dataset.processingOpen = "";
            button.append(processingPlaceholder("media-item-thumb media-item-thumb-fallback"));
        }
        el.append(button);
        return el;
    }

    test("the lightbox leaves out a tile that is still processing", () => {
        const grid = document.createElement("div");
        grid.className = "media-gallery-grid";
        const ready = mediaTile("photo-8", false);
        grid.append(mediaTile("photo-7", true), ready);
        document.body.append(grid);
        const button = ready.querySelector<HTMLElement>("button");
        if (!button) throw new Error("missing button");
        openMediaLightbox(button);
        expect(calls).toHaveLength(1);
        expect(calls[0]?.list.map((item) => item.mediaKey)).toEqual(["photo-8"]);
        expect(calls[0]?.idx).toBe(0);
    });

    test("settling points the tile's media data at the processed file", () => {
        const el = mediaTile("photo-7", true);
        document.body.append(el);
        settleMediaItem(el, READY);
        expect(el.hasAttribute("data-processing")).toBe(false);
        expect(el.dataset.mediaUrl).toBe(READY.url);
        expect(el.dataset.mediaThumb).toBe(READY.thumb_url);
        expect(el.querySelector("img.media-item-thumb")?.getAttribute("src")).toBe(READY.thumb_url);
        expect(el.querySelector("button")?.disabled).toBe(false);
    });

    test("a tile appended by a later provider load is watched", async () => {
        const grid = document.createElement("div");
        document.body.append(grid);
        observeMediaGalleryProcessing(grid);
        grid.append(mediaTile("photo-7", true));
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(window.urbanlensProcessingPollers?.get("/vault/photos/processing/")?.size).toBe(1);
    });
});
