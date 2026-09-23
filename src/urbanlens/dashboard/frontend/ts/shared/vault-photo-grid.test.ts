/**
 * Vault Photos' windowed grid, bound the way the page binds it: tiles far from the viewport drop their bitmap,
 * and get it back once they come near again (P59).
 */

import { afterAll, beforeAll, describe, expect, it } from "bun:test";

import { UNLOAD_BUFFER_PX } from "./photo-virtual-grid";

const VIEWPORT = 700;
const ROW = 123;
const COLUMNS = 3;
const FIRST_PAGE = 24;
const TOTAL = 46;

let scrollY = 0;
let aboveGrid = 400;
let intersect: (() => void) | null = null;

const realRect = HTMLElement.prototype.getBoundingClientRect;
const realRaf = window.requestAnimationFrame;
const realFetch = globalThis.fetch;
const realObserver = (globalThis as Record<string, unknown>).IntersectionObserver;
const realInnerHeight = Object.getOwnPropertyDescriptor(window, "innerHeight");

function thumb(id: number): string {
    return `https://urbanlens.test/media/pin_images/thumbs/t/${id}-thumb.webp`;
}

function tileHtml(id: number): string {
    return `<li class="photo-tile" id="photo-tile-${id}" data-id="${id}"><button type="button" class="photo-tile-btn"><img src="${thumb(id)}" alt=""></button></li>`;
}

function imgs(): HTMLImageElement[] {
    return Array.from(document.querySelectorAll<HTMLImageElement>("#photo-grid .photo-tile img"));
}

/** Scroll the page to *pageY* and let the grid react. */
function scrollTo(pageY: number): void {
    scrollY = pageY;
    window.dispatchEvent(new Event("scroll"));
}

beforeAll(async () => {
    Object.defineProperty(window, "innerHeight", { configurable: true, value: VIEWPORT });
    window.requestAnimationFrame = (cb: FrameRequestCallback) => {
        cb(0);
        return 1;
    };
    (globalThis as Record<string, unknown>).IntersectionObserver = class {
        constructor(callback: (entries: { isIntersecting: boolean }[]) => void) {
            intersect = () => callback([{ isIntersecting: true }]);
        }
        observe(): void {}
        disconnect(): void {}
    };
    globalThis.fetch = (async () =>
        new Response(
            JSON.stringify({
                items: Array.from({ length: TOTAL - FIRST_PAGE }, (_, i) => ({ id: FIRST_PAGE + i + 1, url: thumb(FIRST_PAGE + i + 1), thumb_url: thumb(FIRST_PAGE + i + 1) })),
            }),
        )) as unknown as typeof fetch;
    HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement): DOMRect {
        const tile = this.closest<HTMLElement>(".photo-tile[data-id]");
        const index = tile ? Array.from(document.querySelectorAll(".photo-tile[data-id]")).indexOf(tile) : 0;
        const top = aboveGrid + Math.floor(index / COLUMNS) * ROW - scrollY;
        return { top, bottom: top + ROW - 5, left: 0, right: 0, width: 0, height: ROW - 5, x: 0, y: top, toJSON: () => ({}) } as DOMRect;
    };

    document.body.innerHTML =
        `<ul id="photo-grid" data-items-url="/vault/photos/items/" data-photo-count="${TOTAL}">` +
        Array.from({ length: FIRST_PAGE }, (_, i) => tileHtml(i + 1)).join("") +
        "</ul>";
    await import("./vault-photo-grid");
    intersect?.();
    await new Promise((resolve) => setTimeout(resolve, 0));
});

afterAll(() => {
    HTMLElement.prototype.getBoundingClientRect = realRect;
    window.requestAnimationFrame = realRaf;
    globalThis.fetch = realFetch;
    (globalThis as Record<string, unknown>).IntersectionObserver = realObserver;
    if (realInnerHeight) Object.defineProperty(window, "innerHeight", realInnerHeight);
    document.body.innerHTML = "";
});

describe("vault photo grid pruning", () => {
    it("loads the second page", () => {
        expect(imgs()).toHaveLength(TOTAL);
        expect(document.querySelector(".photo-grid-sentinel")).toBeNull();
    });

    it("drops the first tiles' bitmaps at the bottom of the grid, and restores them back at the top", () => {
        aboveGrid = 400;
        scrollTo(aboveGrid + Math.ceil(TOTAL / COLUMNS) * ROW);
        const first = imgs()[0]!;
        expect(first.getAttribute("src")).toBeNull();
        expect(first.dataset.src).toBe(thumb(1));

        scrollTo(0);

        expect(first.getAttribute("src")).toBe(thumb(1));
    });

    it("keeps a tile pruned while the page top is still more than the buffer above it", () => {
        // What the Vault page looks like at a phone's width once its album list has rendered above the grid.
        aboveGrid = VIEWPORT + UNLOAD_BUFFER_PX + 4000;
        scrollTo(aboveGrid + Math.ceil(TOTAL / COLUMNS) * ROW);
        const first = imgs()[0]!;
        expect(first.getAttribute("src")).toBeNull();

        scrollTo(0);
        expect(first.getAttribute("src")).toBeNull();

        scrollTo(aboveGrid - VIEWPORT / 2);
        expect(first.getAttribute("src")).toBe(thumb(1));
    });
});
