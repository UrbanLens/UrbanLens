/**
 * The Vault gallery uploader shared by Photos and Documents: prechecks, uploads, deletes, and the lightbox list.
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import type { LightboxInput } from "./photo-tile";
import { documentLightboxItem, DOCUMENTS, renderVaultDocumentTile } from "./vault-document-grid";
import { renderVaultSkeletonTile, VaultGrid, type VaultKind } from "./vault-media-grid";
import { PHOTOS, photoLightboxItem, renderVaultPhotoTile } from "./vault-photo-grid";
import { bindUploadRetry, formatBytes, precheckError, VaultUploader, type VaultUploaderOptions } from "./vault-uploader";

interface Toasts {
    success: string[];
    error: string[];
    warning: string[];
    info: string[];
}

interface Request {
    url: string;
    method: string;
    body: FormData | null;
}

let toasts: Toasts;
let requests: Request[];
let respond: (request: Request) => Response;
const realFetch = globalThis.fetch;
const realConfirm = window.confirm;
const realToastr = window.toastr;
const realCsrfToken = window.csrftoken;
const realHtmx = window.htmx;
const realOpenLightboxItem = window.galleryOpenLightboxItem;

function file(name: string, type: string, size = 10): File {
    return new File([new Uint8Array(size)], name, { type });
}

/** The page shape `media_page.html` renders; the Photos page puts its Albums head before the gallery's. */
function page(kind: VaultKind, { tiles = [1, 2], quota = "", albumsHead = kind === PHOTOS } = {}): void {
    const head = albumsHead ? '<section id="albums"><div class="photos-gallery-head"><h2>Albums</h2></div></section>' : "";
    const grid = tiles.length
        ? `<ul class="photos-grid" id="${kind.kind}-grid" data-items-url="/vault/${kind.plural}/items/" data-photo-count="${tiles.length}">` +
          tiles.map((id) => `<li class="${kind.kind}-tile" id="${kind.kind}-tile-${id}" data-id="${id}" data-url="/m/${id}"></li>`).join("") +
          "</ul>"
        : "";
    document.body.innerHTML =
        `<div id="${kind.plural}-page" data-upload-url="/vault/${kind.plural}/upload/" data-action-base="/vault/photos/" data-storage-used="100" data-storage-quota="${quota}" data-max-file-size="1000">` +
        `<input type="file" id="${kind.plural}-file-input">` +
        `<div id="${kind.plural}-upload-progress" hidden><div id="${kind.plural}-upload-bar"></div></div>` +
        head +
        `<section class="photos-gallery"><div class="photos-gallery-head"><h2>All</h2>${tiles.length ? `<span class="badge">${tiles.length}</span>` : ""}` +
        '<a class="photos-show-others-toggle"><span class="badge">7</span></a></div>' +
        grid +
        "</section></div>" +
        `<div id="${kind.plural}-drop-overlay" hidden></div>`;
}

function photoOptions(): VaultUploaderOptions {
    return {
        kind: PHOTOS,
        field: "image",
        accepts: (f) => f.type.startsWith("image/"),
        refusedMessage: (refused) => `${refused.length} refused`,
        uploadedMessage: (count) => `${count} uploaded`,
        lightboxItem: photoLightboxItem,
        companionIds: (id) => [`photo-card-${id}`],
    };
}

function gridOf(kind: VaultKind): VaultGrid | null {
    return VaultGrid.find({ kind, renderTile: kind === PHOTOS ? renderVaultPhotoTile : renderVaultDocumentTile, imageSelector: null });
}

function uploader(kind: VaultKind = PHOTOS, options: VaultUploaderOptions = photoOptions(), grid = gridOf(kind)): VaultUploader {
    return new VaultUploader(document.getElementById(`${kind.plural}-page`)!, grid, options);
}

function badges(): string[] {
    return Array.from(document.querySelectorAll(".photos-gallery-head > .badge"), (b) => `${b.closest("section")?.id || "gallery"}:${b.textContent}`);
}

beforeEach(() => {
    toasts = { success: [], error: [], warning: [], info: [] };
    window.toastr = {
        success: (m) => void toasts.success.push(m),
        error: (m) => void toasts.error.push(m),
        warning: (m) => void toasts.warning.push(m),
        info: (m) => void toasts.info.push(m),
        clear: () => undefined,
    };
    window.csrftoken = "tok";
    requests = [];
    respond = () => new Response(JSON.stringify({ id: 99, url: "/m/99", thumb_url: "/t/99" }), { status: 201 });
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
        const request = { url: String(input), method: init?.method ?? "GET", body: (init?.body as FormData | undefined) ?? null };
        requests.push(request);
        return respond(request);
    }) as typeof fetch;
});

afterEach(() => {
    globalThis.fetch = realFetch;
    window.confirm = realConfirm;
    window.toastr = realToastr;
    window.csrftoken = realCsrfToken;
    window.htmx = realHtmx;
    window.galleryOpenLightboxItem = realOpenLightboxItem;
    document.body.innerHTML = "";
});

describe("formatBytes", () => {
    test("whole bytes, then one decimal per unit", () => {
        expect(formatBytes(512)).toBe("512 bytes");
        expect(formatBytes(1536)).toBe("1.5 KB");
        expect(formatBytes(5 * 1024 ** 3)).toBe("5.0 GB");
    });
});

describe("precheckError", () => {
    const limits = { maxFileSize: 1000, used: 100, quota: 500 };

    test("a file over the size limit is refused whatever the quota", () => {
        expect(precheckError(file("a.jpg", "image/jpeg", 1001), 0, { ...limits, quota: null })).toContain("too large");
    });

    test("the quota counts what the same batch has already committed", () => {
        expect(precheckError(file("a.jpg", "image/jpeg", 300), 0, limits)).toBeNull();
        expect(precheckError(file("a.jpg", "image/jpeg", 300), 150, limits)).toContain("exceed your storage quota");
    });

    test("no quota means no quota check", () => {
        expect(precheckError(file("a.jpg", "image/jpeg", 999), 10_000, { ...limits, quota: null })).toBeNull();
    });
});

describe("uploading", () => {
    test("files the page does not take are warned about and never sent", async () => {
        page(PHOTOS);
        await uploader().handleFiles([file("deed.pdf", "application/pdf")]);
        expect(toasts.warning).toEqual(["1 refused"]);
        expect(requests).toEqual([]);
    });

    test("a batch that would pass the quota is cut at the file that would not", async () => {
        page(PHOTOS, { quota: "500" });
        await uploader().handleFiles([file("a.jpg", "image/jpeg", 300), file("b.jpg", "image/jpeg", 300)]);
        expect(requests).toHaveLength(1);
        expect(toasts.error).toHaveLength(1);
        expect(toasts.error[0]).toStartWith("b.jpg: ");
    });

    test("each file is posted under the kind's field with the CSRF token", async () => {
        page(DOCUMENTS);
        const docs: VaultUploaderOptions = { ...photoOptions(), kind: DOCUMENTS, field: "document", accepts: () => true, lightboxItem: documentLightboxItem };
        await uploader(DOCUMENTS, docs).handleFiles([file("deed.pdf", "application/pdf")]);
        expect(requests[0]?.url).toBe("/vault/documents/upload/");
        expect(requests[0]?.body?.get("document")).toBeInstanceOf(File);
        expect(requests[0]?.body?.get("csrfmiddlewaretoken")).toBe("tok");
    });

    test("an upload lands at the front of the grid and in the gallery's own count", async () => {
        page(PHOTOS);
        await uploader().handleFiles([file("a.jpg", "image/jpeg")]);
        const grid = document.getElementById("photo-grid")!;
        expect(grid.firstElementChild?.id).toBe("photo-tile-99");
        expect(grid.dataset.photoCount).toBe("3");
        // Not the Albums heading that comes first on the page, and not the "from others" toggle's badge.
        expect(badges()).toEqual(["gallery:3"]);
        expect(document.querySelector(".photos-show-others-toggle .badge")?.textContent).toBe("7");
        expect(toasts.success).toEqual(["1 uploaded"]);
    });

    test("under another sort the grid re-fetches instead of guessing the new tile's place", async () => {
        page(PHOTOS);
        document.body.insertAdjacentHTML("beforeend", '<select id="vault-photos-sort"><option value="name" selected>Name</option></select>');
        const grid = gridOf(PHOTOS)!;
        let refreshed = 0;
        grid.refresh = () => void refreshed++;
        await uploader(PHOTOS, photoOptions(), grid).handleFiles([file("a.jpg", "image/jpeg")]);
        expect(document.getElementById("photo-tile-99")).toBeNull();
        expect(refreshed).toBe(1);
    });

    test("the server's reason is toasted per file, and the batch still finishes", async () => {
        page(PHOTOS);
        let finished = -1;
        respond = () => new Response(JSON.stringify({ error: "That photo is already in your Vault." }), { status: 409 });
        await uploader(PHOTOS, { ...photoOptions(), afterUpload: (n) => void (finished = n) }).handleFiles([file("a.jpg", "image/jpeg")]);
        expect(toasts.error).toEqual(["a.jpg: That photo is already in your Vault."]);
        expect(finished).toBe(0);
        expect(toasts.success).toEqual([]);
    });

    test("picking files uploads them and clears the input for the next pick", async () => {
        page(PHOTOS);
        uploader().bindInputs();
        const input = document.getElementById("photos-file-input") as HTMLInputElement;
        Object.defineProperty(input, "files", { configurable: true, value: [file("a.jpg", "image/jpeg")] });
        input.dispatchEvent(new Event("change"));
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(requests).toHaveLength(1);
    });
});

describe("deleting", () => {
    test("nothing is sent unless the user confirms", async () => {
        page(PHOTOS);
        window.confirm = () => false;
        await uploader().remove(1);
        expect(requests).toEqual([]);
        expect(document.getElementById("photo-tile-1")).not.toBeNull();
    });

    test("a confirmed delete removes the tile, its queue card and one from the count", async () => {
        page(PHOTOS);
        document.body.insertAdjacentHTML("beforeend", '<div id="photo-card-1"></div>');
        window.confirm = () => true;
        respond = () => new Response("", { status: 200 });
        await uploader().remove(1);
        expect(requests[0]).toMatchObject({ url: "/vault/photos/1/delete/", method: "POST" });
        expect(document.getElementById("photo-tile-1")).toBeNull();
        expect(document.getElementById("photo-card-1")).toBeNull();
        expect(badges()).toEqual(["gallery:1"]);
        expect(toasts.info).toEqual(["Photo deleted."]);
    });

    test("a refused delete says so and leaves the tile", async () => {
        page(DOCUMENTS);
        window.confirm = () => true;
        respond = () => new Response("", { status: 404 });
        await uploader(DOCUMENTS, { ...photoOptions(), kind: DOCUMENTS }).remove(2);
        expect(document.getElementById("document-tile-2")).not.toBeNull();
        expect(toasts.error).toEqual(["Could not delete that document."]);
    });
});

describe("the lightbox list", () => {
    let opened: { list: LightboxInput[]; idx: number } | null;

    beforeEach(() => {
        opened = null;
        window.galleryOpenLightboxItem = (list, idx) => {
            opened = { list, idx };
        };
    });

    test("skeleton and still-processing tiles are left out, and the clicked tile is the start", () => {
        page(PHOTOS, { tiles: [1, 2, 3] });
        const grid = document.getElementById("photo-grid")!;
        grid.appendChild(renderVaultSkeletonTile(PHOTOS));
        document.getElementById("photo-tile-2")!.dataset.processing = "pending";
        uploader().openLightbox(3);
        expect(opened!.list.map((item) => item.imageId)).toEqual([1, 3]);
        expect(opened!.idx).toBe(1);
    });

    test("a document opens in the lightbox's document mode", () => {
        page(DOCUMENTS, { tiles: [5] });
        uploader(DOCUMENTS, { ...photoOptions(), kind: DOCUMENTS, lightboxItem: documentLightboxItem }).openLightbox(5);
        expect(opened!.list).toEqual([{ url: "/m/5", caption: "", imageId: 5, mediaType: "document" }]);
    });

    test("a photo item leaves ownership unset, which the lightbox reads as the viewer's own", () => {
        page(PHOTOS, { tiles: [1] });
        uploader().openLightbox(1);
        expect(opened!.list[0]).not.toHaveProperty("isMine");
    });
});

describe("tile controls", () => {
    let opened: number[];

    beforeEach(() => {
        opened = [];
        window.galleryOpenLightboxItem = (list, idx) => void opened.push(list[idx]?.imageId ?? -1);
    });

    function addControls(kind: VaultKind, id: number): void {
        document
            .getElementById(`${kind.kind}-tile-${id}`)
            ?.insertAdjacentHTML("beforeend", `<button type="button" class="${kind.kind}-tile-btn"><img alt=""></button><button type="button" class="${kind.kind}-tile-del"><i>delete</i></button>`);
    }

    function click(selector: string): void {
        document.querySelector(selector)?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    }

    test("a server-rendered tile's open and delete buttons work by their class", async () => {
        page(PHOTOS);
        addControls(PHOTOS, 2);
        uploader().bindTileActions();
        click("#photo-tile-2 .photo-tile-btn img");
        expect(opened).toEqual([2]);

        window.confirm = () => true;
        respond = () => new Response("", { status: 200 });
        click("#photo-tile-2 .photo-tile-del i");
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(requests[0]).toMatchObject({ url: "/vault/photos/2/delete/", method: "POST" });
    });

    test("a tile rendered from JSON opens once per click", () => {
        page(DOCUMENTS, { tiles: [] });
        const grid = document.createElement("ul");
        grid.id = "document-grid";
        document.getElementById("documents-page")?.appendChild(grid);
        const tile = renderVaultDocumentTile({ id: 7, url: "/m/7" });
        if (tile) grid.appendChild(tile);
        uploader(DOCUMENTS, { ...photoOptions(), kind: DOCUMENTS, lightboxItem: documentLightboxItem }).bindTileActions();
        click("#document-tile-7 .document-tile-btn");
        expect(opened).toEqual([7]);
    });
});

describe("retrying a failed upload", () => {
    test("the picked file is re-posted and its card dismissed", async () => {
        const dismissed: Array<[string, string]> = [];
        window.htmx = { process: () => undefined, trigger: () => undefined, ajax: async (verb, url) => void dismissed.push([verb, url]) };
        document.body.innerHTML =
            '<div id="photos-page"><div class="photos-issue-card"><input type="file" data-retry-failure="1" data-retry-url="/retry/" data-dismiss-url="/dismiss/"></div></div>';
        bindUploadRetry(document.getElementById("photos-page")!, "/vault/photos/upload/", "image");
        const input = document.querySelector("input")!;
        Object.defineProperty(input, "files", { configurable: true, value: [file("again.jpg", "image/jpeg")] });
        input.dispatchEvent(new Event("change", { bubbles: true }));
        await new Promise((resolve) => setTimeout(resolve, 0));
        expect(requests[0]?.url).toBe("/retry/");
        expect(requests[0]?.body?.get("image")).toBeInstanceOf(File);
        expect(dismissed).toEqual([["POST", "/dismiss/"]]);
        expect(toasts.success).toEqual(["again.jpg uploaded."]);
    });
});
