import { beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { DmLightbox } from "./dm-lightbox";

const lightbox = new DmLightbox();

beforeAll(() => lightbox.install());

beforeEach(() => {
    document.body.innerHTML = `
        <ul id="dm-messages">
            <li><button type="button" class="dm-bubble__image-link" id="a"><img src="https://x.test/1.jpg"></button></li>
            <li><button type="button" class="dm-bubble__image-link" id="b"><img src="https://x.test/2.jpg"></button>
                <button type="button" class="dm-bubble__image-link" id="c"><img src="https://x.test/3.jpg"></button></li>
        </ul>
        <dialog id="dm-lightbox">
            <button type="button" id="dm-lightbox-prev" data-dm-lightbox-step="-1"></button>
            <img id="dm-lightbox-img" src="">
            <button type="button" id="dm-lightbox-next" data-dm-lightbox-step="1"></button>
            <div id="dm-lightbox-thumbs" hidden></div>
        </dialog>`;
});

function el(id: string): HTMLElement {
    const found = document.getElementById(id);
    if (!found) throw new Error(`#${id} missing`);
    return found;
}

function shown(): string {
    return el("dm-lightbox-img").getAttribute("src") ?? "";
}

describe("DmLightbox", () => {
    test("clicking a thread photo opens on it, with every thread photo to step through", () => {
        el("b").click();
        expect(shown()).toBe("https://x.test/2.jpg");
        expect(lightbox.count).toBe(3);
        expect(el("dm-lightbox-thumbs").hidden).toBe(false);
        expect(el("dm-lightbox-thumbs").children[1]?.classList.contains("dm-lightbox-thumb--active")).toBe(true);
    });

    test("it does not wrap: the arrow at the end hides and stepping past it does nothing", () => {
        el("c").click();
        expect(el("dm-lightbox-next").hidden).toBe(true);
        el("dm-lightbox-next").click();
        expect(shown()).toBe("https://x.test/3.jpg");
        el("dm-lightbox-prev").click();
        expect(shown()).toBe("https://x.test/2.jpg");
        expect(el("dm-lightbox-next").hidden).toBe(false);
    });

    test("a thumbnail jumps to its photo", () => {
        el("a").click();
        const third = el("dm-lightbox-thumbs").children[2];
        if (!(third instanceof HTMLElement)) throw new Error("no third thumb");
        third.click();
        expect(shown()).toBe("https://x.test/3.jpg");
        expect(el("dm-lightbox-prev").hidden).toBe(false);
    });

    test("one photo shows no thumbnail strip", () => {
        document.getElementById("b")?.remove();
        document.getElementById("c")?.remove();
        el("a").click();
        expect(el("dm-lightbox-thumbs").hidden).toBe(true);
        expect(el("dm-lightbox-prev").hidden).toBe(true);
        expect(el("dm-lightbox-next").hidden).toBe(true);
    });
});
