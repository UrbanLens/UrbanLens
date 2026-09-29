/**
 * The Photos tab's album grid pages its server-rendered cards through the shared photo-grid binder (P171).
 */

import { afterEach, beforeEach, describe, expect, it } from "bun:test";

import { albumCardFromJson, bindAlbumGrid } from "./album-grid";

function cardHtml(slug: string): string {
    return `<li class="album-card" data-album-slug="${slug}" data-album-drop><a class="album-card-link" hx-get="/albums/${slug}/">${slug}</a></li>`;
}

const realFetch = globalThis.fetch;
const realObserver = (globalThis as Record<string, unknown>).IntersectionObserver;
const realHtmx = window.htmx;
let intersect: (() => void) | null = null;
let requested: string[] = [];
let processed: Element[] = [];

beforeEach(() => {
    requested = [];
    processed = [];
    (globalThis as Record<string, unknown>).IntersectionObserver = class {
        constructor(callback: (entries: { isIntersecting: boolean }[]) => void) {
            intersect = () => callback([{ isIntersecting: true }]);
        }
        observe(): void {}
        disconnect(): void {}
    };
    globalThis.fetch = (async (url: string) => {
        requested.push(url);
        return new Response(JSON.stringify({ items: [{ slug: "c", html: cardHtml("c") }, { slug: "d", html: cardHtml("d") }], total: 4 }));
    }) as unknown as typeof fetch;
    window.htmx = { process: (el: Element) => processed.push(el), trigger: () => {}, ajax: async () => {} };
});

afterEach(() => {
    globalThis.fetch = realFetch;
    (globalThis as Record<string, unknown>).IntersectionObserver = realObserver;
    window.htmx = realHtmx;
    document.body.innerHTML = "";
});

describe("albumCardFromJson", () => {
    it("builds the card the server rendered", () => {
        const card = albumCardFromJson({ slug: "a", html: cardHtml("a") });
        expect(card?.dataset.albumSlug).toBe("a");
    });

    it("refuses anything that is not an album card", () => {
        expect(albumCardFromJson({ html: "<li class='gallery-item'></li>" })).toBeNull();
        expect(albumCardFromJson({ slug: "a" })).toBeNull();
    });
});

describe("bindAlbumGrid", () => {
    it("loads the next page after the first, and hands the new cards to htmx", async () => {
        document.body.innerHTML = `<ul class="albums-grid" data-album-grid data-items-url="/albums/?children=1&albums=1" data-photo-count="4" data-grid-page-size="2">${cardHtml("a")}${cardHtml("b")}</ul>`;
        const grid = document.querySelector<HTMLElement>("[data-album-grid]")!;

        const unbind = bindAlbumGrid(grid);
        intersect?.();
        await new Promise((resolve) => setTimeout(resolve, 0));

        expect(requested).toEqual(["/albums/?children=1&albums=1&offset=2&limit=2"]);
        expect(Array.from(grid.querySelectorAll<HTMLElement>(".album-card")).map((card) => card.dataset.albumSlug)).toEqual(["a", "b", "c", "d"]);
        expect(processed.map((el) => (el as HTMLElement).dataset.albumSlug)).toEqual(["c", "d"]);
        expect(processed.every((el) => el.isConnected)).toBe(true);
        expect(grid.querySelector(".photo-grid-sentinel")).toBeNull();
        unbind();
    });

    it("fetches nothing when the first page already holds every album", () => {
        document.body.innerHTML = `<ul data-album-grid data-items-url="/albums/?albums=1" data-photo-count="2">${cardHtml("a")}${cardHtml("b")}</ul>`;

        bindAlbumGrid(document.querySelector<HTMLElement>("[data-album-grid]")!)();

        expect(requested).toEqual([]);
    });
});
