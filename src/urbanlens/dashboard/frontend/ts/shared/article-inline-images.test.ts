import { describe, expect, test } from "bun:test";

import { applyInlineImageAttrs, inlineImageSettled, refreshedSrc, refreshInlineImages } from "./article-inline-images";

const LINK = "/media/image/1b4e28ba-2fa1-11d2-883f-0016d3cca427/";

function imageFor(src: string): HTMLImageElement {
    const img = document.createElement("img");
    applyInlineImageAttrs(img, { src, alt: "Boiler", title: null });
    return img;
}

describe("inline article images", () => {
    test("render the node's attributes", () => {
        const img = imageFor(LINK);
        expect(img.getAttribute("src")).toBe(LINK);
        expect(img.alt).toBe("Boiler");
        expect(img.hasAttribute("title")).toBe(false);
    });

    test("a refreshed request carries a query the link ignores", () => {
        expect(refreshedSrc(LINK, 5)).toBe(`${LINK}?v=5`);
        expect(refreshedSrc(`${LINK}?a=1`, 5)).toBe(`${LINK}?a=1&v=5`);
    });

    test("refreshing re-requests every copy of the link and nothing else", () => {
        const container = document.createElement("div");
        const first = imageFor(LINK);
        const second = imageFor(LINK);
        const other = imageFor("/media/image/other/");
        container.append(first, second, other);

        expect(refreshInlineImages(container, LINK, 9)).toBe(2);

        expect(first.getAttribute("src")).toBe(`${LINK}?v=9`);
        expect(second.getAttribute("src")).toBe(`${LINK}?v=9`);
        expect(other.getAttribute("src")).toBe("/media/image/other/");
    });

    test("a rendered article's copy is refreshed too", () => {
        const container = document.createElement("div");
        container.innerHTML = `<p><img src="${LINK}" alt=""></p>`;

        expect(refreshInlineImages(container, LINK, 4)).toBe(1);
        expect(container.querySelector("img")?.getAttribute("src")).toBe(`${LINK}?v=4`);
    });

    test("an unchanged node keeps its refreshed request", () => {
        const img = imageFor(LINK);
        refreshInlineImages(img.ownerDocument.body.appendChild(img).parentElement as HTMLElement, LINK, 3);

        applyInlineImageAttrs(img, { src: LINK, alt: "Boiler room", title: "t" });

        expect(img.getAttribute("src")).toBe(`${LINK}?v=3`);
        expect(img.alt).toBe("Boiler room");
        expect(img.title).toBe("t");
        img.remove();
    });

    test("a node pointed elsewhere requests the new link", () => {
        const img = imageFor(LINK);
        refreshInlineImages(document.body.appendChild(img).parentElement as HTMLElement, LINK, 3);

        applyInlineImageAttrs(img, { src: "/media/image/next/", alt: "", title: null });

        expect(img.getAttribute("src")).toBe("/media/image/next/");
        img.remove();
    });

    test("a settled upload is ready, failed, or gone", () => {
        expect(inlineImageSettled({ id: 1, processing: false, processing_failed: false, url: "/media/pin_images/a.webp" })).toBe("ready");
        expect(inlineImageSettled({ id: 1, processing: false, processing_failed: true })).toBe("failed");
        expect(inlineImageSettled(null)).toBe("failed");
    });
});
