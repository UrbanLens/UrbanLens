import { beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { installCarouselControls } from "./carousel-controls";

let calls: string[] = [];

beforeAll(() => {
    const record = (name: string) => () => void calls.push(name);
    installCarouselControls({
        satellite: { prev: record("sat prev"), next: record("sat next"), start: record("sat start"), drop: (img) => void calls.push(`sat drop ${img.id}`) },
        street: { prev: record("sv prev"), next: record("sv next"), start: record("sv start"), drop: (img) => void calls.push(`sv drop ${img.id}`), showStatic: (button) => void calls.push(`sv static ${button.id}`) },
    });
});

beforeEach(() => {
    calls = [];
    document.body.innerHTML = `
      <div id="satellite-view-section">
        <div class="sat-carousel" id="sat-carousel">
          <div class="sat-slide"><img id="sat-1" class="sat-img" data-carousel-img></div>
          <button id="sat-prev" data-carousel-action="satellite-prev"><i id="sat-prev-icon">chevron_left</i></button>
          <button id="sat-next" data-carousel-action="satellite-next"></button>
        </div>
      </div>
      <div id="street-view-section">
        <div class="sv-carousel" id="sv-carousel">
          <div class="sv-slide"><img id="sv-fallback" class="sv-img sv-img--fallback" hidden><button id="sv-static" data-carousel-action="street-static">Show static image</button></div>
          <div class="sv-slide"><img id="sv-2" class="sv-img" data-carousel-img></div>
          <button id="sv-prev" data-carousel-action="street-prev"></button>
          <button id="sv-next" data-carousel-action="street-next"></button>
        </div>
      </div>
      <img id="unrelated">`;
});

function fail(id: string): void {
    document.getElementById(id)!.dispatchEvent(new Event("error"));
}

describe("the satellite and street-view carousels", () => {
    test("their buttons step and switch to the static image", () => {
        document.getElementById("sat-prev-icon")!.click();
        document.getElementById("sat-next")!.click();
        document.getElementById("sv-prev")!.click();
        document.getElementById("sv-next")!.click();
        document.getElementById("sv-static")!.click();
        expect(calls).toEqual(["sat prev", "sat next", "sv prev", "sv next", "sv static sv-static"]);
    });

    test("an image that fails to load is dropped from its own carousel; others are not touched", () => {
        fail("sat-1");
        fail("sv-2");
        fail("sv-fallback");
        fail("unrelated");
        expect(calls).toEqual(["sat drop sat-1", "sv drop sv-2"]);
    });

    test("each starts when htmx brings it in, and not for other content", () => {
        document.getElementById("satellite-view-section")!.dispatchEvent(new CustomEvent("htmx:load", { bubbles: true }));
        document.getElementById("street-view-section")!.dispatchEvent(new CustomEvent("htmx:load", { bubbles: true }));
        document.getElementById("unrelated")!.dispatchEvent(new CustomEvent("htmx:load", { bubbles: true }));
        expect(calls).toEqual(["sat start", "sv start"]);
    });
});
