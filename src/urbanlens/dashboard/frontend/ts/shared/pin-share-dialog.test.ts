import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { installPinShareDialog, uploadedPhotoTile } from "./pin-share-dialog";

const realFetch = globalThis.fetch;
const realToastr = window.toastr;
let respond: () => Response = () => new Response("{}", { status: 201 });
let errors: string[] = [];

const settle = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

function render(): void {
    document.body.innerHTML = `
      <dialog id="pin-share-dialog" open>
        <form id="pin-share-form" data-pin-name="Old Mill" data-upload-url="/pins/mill/gallery/" data-processing-url="/vault/photos/processing/">
          <input type="hidden" name="profile_id" id="pin-share-profile-id">
          <div id="pin-share-form-fields">
            <input type="search" class="pin-share-friend-filter">
            <ul id="pin-share-friend-list">
              <li class="pin-share-friend-item" data-search="owl"><button type="button" class="pin-share-friend-btn" data-id="7" data-username="owl"><span class="msg-avatar">O</span></button></li>
              <li class="pin-share-friend-item" data-search="fox"><button type="button" class="pin-share-friend-btn" data-id="8" data-username="fox"><span class="msg-avatar">F</span></button></li>
            </ul>
            <textarea id="pin-share-message">Worth a look</textarea>
            <input id="pin-share-custom-name"><button type="button" data-pin-share-action="use-my-name">Mine</button>
            <button type="button" id="pin-share-add-attachment-btn" data-pin-share-action="add-attachment">Attach</button>
            <div id="pin-share-attachments" hidden>
              <input type="hidden" name="markup_map_uuid" id="pin-share-map-uuid">
              <div id="pin-share-map-grid">
                <div class="pin-share-map-tile" data-uuid="m1"><button type="button" class="pin-share-map-select-btn"><i>check</i></button></div>
              </div>
              <ul id="pin-share-photo-grid">
                <li class="pin-share-photo"><label><input type="checkbox" name="image_ids" value="3" checked></label></li>
                <li class="pin-share-photo-upload"><button type="button" data-pin-share-action="upload">Upload</button></li>
              </ul>
              <input type="file" id="pin-share-photo-input" multiple hidden>
            </div>
          </div>
          <div id="pin-share-review" hidden>
            <div id="pin-share-review-recipient"></div>
            <p id="pin-share-review-message" hidden></p>
            <li id="pin-share-review-name" hidden><strong id="pin-share-review-name-value"></strong></li>
            <li id="pin-share-review-photos" hidden><strong id="pin-share-review-photo-count"></strong></li>
            <li id="pin-share-review-map" hidden></li>
          </div>
          <div id="pin-share-form-actions"><button type="button" data-pin-share-action="review">Review</button></div>
          <div id="pin-share-review-actions" hidden><button type="button" data-pin-share-action="back">Back</button></div>
        </form>
      </dialog>`;
}

function click(selector: string): void {
    document.querySelector<HTMLElement>(selector)?.click();
}

function hidden(id: string): boolean {
    return document.getElementById(id)?.hidden === true;
}

beforeAll(() => {
    installPinShareDialog();
    installPinShareDialog();
    globalThis.fetch = Object.assign(async () => respond(), realFetch);
});

afterAll(() => {
    globalThis.fetch = realFetch;
    window.toastr = realToastr;
});

beforeEach(() => {
    errors = [];
    window.toastr = { success: () => undefined, error: (m) => void errors.push(m), warning: () => undefined, info: () => undefined, clear: () => undefined };
    render();
});

describe("choosing", () => {
    test("the filter narrows the friends; picking one selects only them", () => {
        const filter = document.querySelector<HTMLInputElement>(".pin-share-friend-filter");
        if (!filter) throw new Error("no filter");
        filter.value = "FO";
        filter.dispatchEvent(new Event("input", { bubbles: true }));
        expect(Array.from(document.querySelectorAll<HTMLElement>(".pin-share-friend-item"), (li) => li.hidden)).toEqual([true, false]);
        click('.pin-share-friend-btn[data-id="7"]');
        click('.pin-share-friend-btn[data-id="8"]');
        expect(document.querySelector<HTMLInputElement>("#pin-share-profile-id")?.value).toBe("8");
        expect(document.querySelectorAll(".pin-share-friend-btn.is-selected").length).toBe(1);
    });

    test("a map toggles on and off", () => {
        click(".pin-share-map-select-btn i");
        expect(document.querySelector<HTMLInputElement>("#pin-share-map-uuid")?.value).toBe("m1");
        click(".pin-share-map-select-btn");
        expect(document.querySelector<HTMLInputElement>("#pin-share-map-uuid")?.value).toBe("");
        expect(document.querySelector(".pin-share-map-tile.is-selected")).toBeNull();
    });

    test("Use mine fills in the pin's own name; Attach reveals the attachments", () => {
        click('[data-pin-share-action="use-my-name"]');
        expect(document.querySelector<HTMLInputElement>("#pin-share-custom-name")?.value).toBe("Old Mill");
        click('[data-pin-share-action="add-attachment"]');
        expect(hidden("pin-share-attachments")).toBe(false);
        expect(hidden("pin-share-add-attachment-btn")).toBe(true);
    });
});

describe("review", () => {
    test("needs a friend first", () => {
        click('[data-pin-share-action="review"]');
        expect(errors).toEqual(["Choose a friend to share with."]);
        expect(hidden("pin-share-review")).toBe(true);
    });

    test("lists what will go, and Back returns to the form", () => {
        click('.pin-share-friend-btn[data-id="7"]');
        const name = document.querySelector<HTMLInputElement>("#pin-share-custom-name");
        if (name) name.value = "<b>Mill</b>";
        click('[data-pin-share-action="review"]');
        expect(hidden("pin-share-review")).toBe(false);
        expect(hidden("pin-share-form-fields")).toBe(true);
        expect(document.getElementById("pin-share-review-recipient")?.textContent).toBe("Oowl");
        expect(document.getElementById("pin-share-review-message")?.textContent).toBe("Worth a look");
        expect(document.getElementById("pin-share-review-name-value")?.textContent).toBe('"<b>Mill</b>"');
        expect(document.getElementById("pin-share-review-photo-count")?.textContent).toBe("1");
        expect(hidden("pin-share-review-map")).toBe(true);
        click('[data-pin-share-action="back"]');
        expect(hidden("pin-share-review")).toBe(true);
        expect(hidden("pin-share-form-fields")).toBe(false);
    });
});

describe("uploads", () => {
    test("a photo still being processed shows a placeholder the page-wide watch will settle", () => {
        const tile = uploadedPhotoTile({ id: 12, url: "", processing: true }, "/vault/photos/processing/");
        const label = tile.querySelector("label");
        expect(tile.querySelector("img")).toBeNull();
        expect(label?.dataset.processingAuto).toBe("");
        expect(label?.dataset.processing).toBe("pending");
        expect(label?.dataset.processingUrl).toBe("/vault/photos/processing/");
        expect(tile.querySelector<HTMLInputElement>('input[name="image_ids"]')?.checked).toBe(true);
    });

    test("a ready photo shows its thumbnail, set as a URL rather than markup", () => {
        const tile = uploadedPhotoTile({ id: 13, thumb_url: '/t.jpg" onerror="alert(1)', url: "/f.jpg" }, "");
        const img = tile.querySelector("img");
        expect(img?.getAttribute("src")).toBe('/t.jpg" onerror="alert(1)');
        expect(img?.hasAttribute("onerror")).toBe(false);
    });

    test("an upload lands before the Upload tile, chosen; a refusal says why", async () => {
        respond = () => new Response(JSON.stringify({ id: 14, thumb_url: "/t14.jpg", url: "/f14.jpg" }), { status: 201 });
        const input = document.getElementById("pin-share-photo-input");
        if (!(input instanceof HTMLInputElement)) throw new Error("no input");
        Object.defineProperty(input, "files", { value: [new File(["x"], "a.jpg")], configurable: true });
        input.dispatchEvent(new Event("change", { bubbles: true }));
        await settle();
        const ids = Array.from(document.querySelectorAll<HTMLInputElement>('#pin-share-photo-grid input[name="image_ids"]'), (i) => i.value);
        expect(ids).toEqual(["3", "14"]);
        respond = () => new Response(JSON.stringify({ error: "You already added this photo." }), { status: 409 });
        input.dispatchEvent(new Event("change", { bubbles: true }));
        await settle();
        expect(errors).toEqual(["Upload failed: You already added this photo."]);
    });
});
