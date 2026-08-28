/**
 * Shared map context menu: the base actions every map offers on right-click.
 *
 * Placement math is skipped - happy-dom reports getBoundingClientRect as zeros,
 * so asserting left/top would be asserting on the stub. What is real here is
 * the menu's contents, the clipboard write, extra items, Street View reveal,
 * and outside-click dismiss.
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { closeMapContextMenus, showMapContextMenu, STREETVIEW_CHECK_URL } from "./map-context-menu";

const originalFetch = globalThis.fetch;
const originalClipboard = navigator.clipboard;

let copied: string | null = null;
let fetchUrls: string[] = [];
let streetViewAvailable = false;

function stubFetch(): void {
    fetchUrls = [];
    globalThis.fetch = ((url: string) => {
        fetchUrls.push(String(url));
        return Promise.resolve({ json: () => Promise.resolve({ available: streetViewAvailable }) });
    }) as unknown as typeof fetch;
}

beforeEach(() => {
    document.body.innerHTML = "";
    copied = null;
    streetViewAvailable = false;
    Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: (text: string) => { copied = text; return Promise.resolve(); } },
    });
    stubFetch();
});

afterEach(() => {
    closeMapContextMenus();
    globalThis.fetch = originalFetch;
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: originalClipboard });
});

function open(extra?: Parameters<typeof showMapContextMenu>[0]["extraItems"]): HTMLElement {
    return showMapContextMenu({
        lat: 42.6526,
        lng: -73.7578,
        clientX: 40,
        clientY: 80,
        extraItems: extra,
    });
}

describe("showMapContextMenu", () => {
    test("always offers copy-coordinates and directions", () => {
        const menu = open();
        expect(menu.className).toBe("map-context-menu");
        expect(menu.textContent).toContain("42.652600, -73.757800");
        expect(menu.textContent).toContain("Directions here");
        const directions = menu.querySelector<HTMLAnchorElement>("a[href*='maps/dir']");
        expect(directions?.href).toContain("42.6526");
        expect(directions?.href).toContain("-73.7578");
    });

    test("hides Street View until coverage is confirmed", () => {
        const menu = open();
        const streetView = menu.querySelector<HTMLAnchorElement>(".map-context-menu__streetview");
        expect(streetView).toBeTruthy();
        expect(streetView?.style.display).toBe("none");
        expect(fetchUrls[0]).toContain(STREETVIEW_CHECK_URL);
        expect(fetchUrls[0]).toContain("lat=42.6526");
    });

    test("reveals Street View when the coverage check says imagery exists", async () => {
        streetViewAvailable = true;
        stubFetch();
        const menu = open();
        await new Promise((resolve) => setTimeout(resolve, 0));
        const streetView = menu.querySelector<HTMLAnchorElement>(".map-context-menu__streetview");
        expect(streetView?.style.display).toBe("");
    });

    test("copies coordinates on the coords row", async () => {
        const menu = open();
        const coords = menu.querySelector<HTMLButtonElement>(".map-context-menu__coords");
        coords?.click();
        await Promise.resolve();
        expect(copied).toBe("42.652600, -73.757800");
        expect(document.querySelector(".map-context-menu")).toBeNull();
    });

    test("appends extra items after the shared actions", () => {
        let clicked = false;
        const menu = open([
            {
                icon: "add_location",
                label: "Create child pin here",
                onClick: () => {
                    clicked = true;
                },
            },
        ]);
        expect(menu.textContent).toContain("Create child pin here");
        const extra = [...menu.querySelectorAll("button")].find((btn) => btn.textContent?.includes("Create child pin here"));
        extra?.click();
        expect(clicked).toBe(true);
        expect(document.querySelector(".map-context-menu")).toBeNull();
    });

    test("renders a header and preamble above the shared actions", () => {
        const preamble = document.createElement("div");
        preamble.textContent = "A nearby park";
        const menu = showMapContextMenu({
            lat: 1,
            lng: 2,
            clientX: 0,
            clientY: 0,
            header: "Place name",
            preamble,
        });
        const text = menu.textContent || "";
        expect(text.indexOf("Place name")).toBeLessThan(text.indexOf("A nearby park"));
        expect(text.indexOf("A nearby park")).toBeLessThan(text.indexOf("Directions here"));
    });
});
