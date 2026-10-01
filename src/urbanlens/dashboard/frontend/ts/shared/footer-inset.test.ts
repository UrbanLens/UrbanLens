import { afterEach, describe, expect, test } from "bun:test";

import { FOOTER_HEIGHT_PROPERTY, installGlobalFooterInset } from "./footer-inset";

type Callback = () => void;

const realResizeObserver = globalThis.ResizeObserver;
let observed: { target: Element; callback: Callback }[] = [];

function fakeResizeObserver(): void {
    observed = [];
    globalThis.ResizeObserver = class {
        constructor(private readonly callback: Callback) {}
        observe(target: Element): void {
            observed.push({ target, callback: this.callback });
        }
        unobserve(): void {}
        disconnect(): void {}
    } as unknown as typeof ResizeObserver;
}

function footerOfHeight(height: number): HTMLElement {
    document.body.innerHTML = `<footer class="page-footer"></footer>`;
    const footer = document.querySelector(".page-footer") as HTMLElement;
    footer.getBoundingClientRect = () => ({ height }) as DOMRect;
    return footer;
}

afterEach(() => {
    globalThis.ResizeObserver = realResizeObserver;
    document.documentElement.style.removeProperty(FOOTER_HEIGHT_PROPERTY);
});

describe("footer inset", () => {
    test("publishes the footer's height, rounded up", () => {
        fakeResizeObserver();
        footerOfHeight(86.4);

        installGlobalFooterInset();

        expect(document.documentElement.style.getPropertyValue(FOOTER_HEIGHT_PROPERTY)).toBe("87px");
    });

    test("follows the footer when it wraps to another line", () => {
        fakeResizeObserver();
        const footer = footerOfHeight(47);
        installGlobalFooterInset();

        footer.getBoundingClientRect = () => ({ height: 102 }) as DOMRect;
        for (const entry of observed.filter((o) => o.target === footer)) entry.callback();

        expect(document.documentElement.style.getPropertyValue(FOOTER_HEIGHT_PROPERTY)).toBe("102px");
    });

    test("a page without the footer leaves the fallback in charge", () => {
        fakeResizeObserver();
        document.body.innerHTML = "";

        installGlobalFooterInset();

        expect(document.documentElement.style.getPropertyValue(FOOTER_HEIGHT_PROPERTY)).toBe("");
    });
});
