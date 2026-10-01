import { afterEach, beforeAll, expect, test } from "bun:test";

import { installVisitPhotos } from "./visit-photos";

const realOpen = window.galleryOpenLightboxItem;
let opened: { items: unknown[]; index: number }[] = [];

beforeAll(() => installVisitPhotos());

afterEach(() => {
    window.galleryOpenLightboxItem = realOpen;
    opened = [];
});

test("a visit's photo opens the site lightbox on that visit's photos, view-only", () => {
    window.galleryOpenLightboxItem = (items, index) => void opened.push({ items, index });
    document.body.innerHTML = `
      <div class="visit-photos" id="v1">
        <a href="/m/1.jpg" class="visit-photo-thumb" data-full="/m/1.jpg" data-caption="Stairs"><img></a>
        <a class="visit-photo-thumb" data-caption="Processing" data-processing="pending"><span></span></a>
        <a href="/m/3.jpg" class="visit-photo-thumb" data-full="/m/3.jpg" data-caption="Roof" id="roof"><img></a>
      </div>
      <div class="visit-photos"><a href="/m/9.jpg" class="visit-photo-thumb" data-full="/m/9.jpg"><img></a></div>`;
    const click = new MouseEvent("click", { bubbles: true, cancelable: true });
    document.querySelector("#roof img")?.dispatchEvent(click);
    expect(click.defaultPrevented).toBe(true);
    expect(opened).toEqual([
        {
            items: [
                { url: "/m/1.jpg", caption: "Stairs" },
                { url: "/m/3.jpg", caption: "Roof" },
            ],
            index: 1,
        },
    ]);
});

test("a photo still processing opens nothing until it has a file", () => {
    window.galleryOpenLightboxItem = (items, index) => void opened.push({ items, index });
    document.body.innerHTML = `<div class="visit-photos"><a class="visit-photo-thumb" id="p" data-processing="pending"><span></span></a></div>`;
    document.getElementById("p")?.click();
    expect(opened).toEqual([]);
    const settled = document.getElementById("p");
    settled?.setAttribute("data-url", "/m/7.jpg");
    settled?.click();
    expect(opened).toEqual([{ items: [{ url: "/m/7.jpg", caption: "" }], index: 0 }]);
});
