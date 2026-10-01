import { beforeEach, describe, expect, test } from "bun:test";

import { PendingAttachments, shareFromPick } from "./dm-attachments";
import { byId } from "./dom";

function el(id: string): HTMLElement {
    const found = document.getElementById(id);
    if (!found) throw new Error(`#${id} missing`);
    return found;
}

beforeEach(() => {
    document.body.innerHTML = `
        <div id="dm-composer-image-chips"></div>
        <span id="dm-map-chip" hidden><span id="dm-map-chip-thumb"></span></span>
        <span id="dm-share-chip" hidden><i id="dm-share-chip-icon"></i><span id="dm-share-chip-label"></span></span>
        <input type="hidden" id="dm-markup-map-uuid" value="">
        <dialog class="dm-share-dialog" data-kind="pin" data-field-name="pin_slug">
            <button type="button" id="pick" class="dm-share-pick-btn" data-value="old-mill" data-label="<b>Old Mill</b>" data-lat="42.5" data-lng="-71.25"></button>
            <button type="button" id="pick-no-coords" class="dm-share-pick-btn" data-value="x" data-label="X" data-lat="" data-lng="None"></button>
        </dialog>
        <dialog class="dm-share-dialog" data-kind="other" data-field-name="x"><button type="button" id="pick-bad" data-value="v"></button></dialog>`;
});

describe("shareFromPick", () => {
    test("reads the kind and field from the dialog and the rest from the button", () => {
        expect(shareFromPick(el("pick"))).toEqual({ kind: "pin", fieldName: "pin_slug", value: "old-mill", label: "<b>Old Mill</b>", lat: 42.5, lng: -71.25 });
        expect(shareFromPick(el("pick-no-coords"))).toMatchObject({ lat: null, lng: null });
    });

    test("a dialog of an unknown kind stages nothing", () => {
        expect(shareFromPick(el("pick-bad"))).toBeNull();
    });
});

describe("PendingAttachments", () => {
    test("a staged share shows as a chip, its label as text", () => {
        const pending = new PendingAttachments();
        pending.setShare(shareFromPick(el("pick")));
        expect(el("dm-share-chip").hidden).toBe(false);
        expect(el("dm-share-chip-icon").textContent).toBe("location_on");
        expect(el("dm-share-chip-label").textContent).toBe("<b>Old Mill</b>");
        expect(el("dm-share-chip-label").querySelector("b")).toBeNull();
        expect(pending.isEmpty).toBe(false);
        pending.reset();
        expect(el("dm-share-chip").hidden).toBe(true);
        expect(pending.isEmpty).toBe(true);
    });

    test("a photo chip is a placeholder until it has a url, and its remove button drops only it", () => {
        const pending = new PendingAttachments();
        pending.addImage({ id: 1, url: "", failed: false }, false);
        pending.addImage({ id: 2, url: "https://x.test/2.jpg", failed: false }, false);
        const chips = () => Array.from(el("dm-composer-image-chips").children);
        expect(chips()[0]?.querySelector('.media-processing[role="img"]')).not.toBeNull();
        expect(chips()[1]?.querySelector("img")?.getAttribute("src")).toBe("https://x.test/2.jpg");
        chips()[0]?.querySelector("button")?.click();
        expect(pending.images.map((i) => i.id)).toEqual([2]);
        expect(chips().length).toBe(1);
    });

    test("an attached map shows its chip and fills the form's field", () => {
        const pending = new PendingAttachments();
        pending.setMap("m-1");
        expect(el("dm-map-chip").hidden).toBe(false);
        expect(byId("dm-markup-map-uuid", HTMLInputElement)?.value).toBe("m-1");
        pending.setMap(null);
        expect(el("dm-map-chip").hidden).toBe(true);
    });
});
