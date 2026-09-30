import { beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { installGlobalCoverHero, updateCoverHero } from "./cover-hero";

beforeAll(() => installGlobalCoverHero());

function el(id: string): HTMLElement {
    const found = document.getElementById(id);
    if (!found) throw new Error(`#${id} missing`);
    return found;
}

beforeEach(() => {
    document.body.innerHTML = `
        <script type="application/json" id="wiki-cover-candidates">[{"id": 2, "url": "/b.jpg"}, {"id": 3, "url": "/c.jpg"}]</script>
        <div class="cover-hero" id="wiki-cover-hero" style="background-image: url('/a.jpg')">
            <button id="prev" data-cover-hero-key="wiki" data-cover-hero-step="-1"></button>
            <button id="next" data-cover-hero-key="wiki" data-cover-hero-step="1"></button>
        </div>
        <nav class="page-tabs"></nav>`;
});

describe("previewing", () => {
    test("the arrows cycle through the other photos and back to the true cover", () => {
        const bg = () => el("wiki-cover-hero").style.backgroundImage;
        const original = bg();
        el("next").click();
        expect(bg()).toContain("/b.jpg");
        el("next").click();
        expect(bg()).toContain("/c.jpg");
        el("next").click();
        expect(bg()).toBe(original);
        el("prev").click();
        expect(bg()).toContain("/c.jpg");
    });
});

describe("updateCoverHero", () => {
    test("a first cover creates the hero before its anchor, with working controls", () => {
        el("wiki-cover-hero").remove();
        updateCoverHero("wiki", "/new.jpg", true);
        const hero = el("wiki-cover-hero");
        expect(hero.nextElementSibling?.classList.contains("page-tabs")).toBe(true);
        expect(hero.style.backgroundImage).toContain("/new.jpg");
        expect(hero.querySelectorAll("[data-cover-hero-step]").length).toBe(2);
        expect(hero.querySelector("[data-cover-photo-remove]")).not.toBeNull();
    });

    test("a cleared cover, or one the viewer hides, removes the hero", () => {
        updateCoverHero("wiki", "/x.jpg", false);
        expect(document.getElementById("wiki-cover-hero")).toBeNull();
    });

    test("a changed cover is what the preview cycle returns to", () => {
        el("next").click();
        updateCoverHero("wiki", "/d.jpg", true);
        el("next").click();
        el("next").click();
        el("next").click();
        expect(el("wiki-cover-hero").style.backgroundImage).toContain("/d.jpg");
    });

    test("a URL with a quote cannot break out of the background", () => {
        updateCoverHero("wiki", "/a'); background: red; x('.jpg", true);
        expect(el("wiki-cover-hero").style.backgroundColor).toBe("");
    });

    test("the pin page's hero is the page hero, toggled rather than created", () => {
        document.body.innerHTML = '<section id="pin-detail-hero" class="ul-page-hero is-adjusting-cover"></section>';
        updateCoverHero("pin", "/p.jpg", true);
        expect(el("pin-detail-hero").classList.contains("ul-page-hero--cover")).toBe(true);
        updateCoverHero("pin", "", true);
        expect(el("pin-detail-hero").className).toBe("ul-page-hero");
        expect(el("pin-detail-hero").style.backgroundImage).toBe("");
    });
});

describe("framing", () => {
    const KEY = "ul_cover_hero_pin_old-mill";
    const HERO = `
        <section class="ul-page-hero ul-page-hero--cover" id="pin-detail-hero" data-cover-state-key="${KEY}">
            <button type="button" id="adjust" data-cover-adjust-toggle>Adjust</button>
            <div id="controls" data-cover-adjust-controls hidden><input type="range" min="100" max="300" value="100" data-cover-zoom></div>
        </section>`;
    const hero = () => el("pin-detail-hero");
    const zoom = () => document.querySelector<HTMLInputElement>("[data-cover-zoom]");

    beforeEach(() => {
        localStorage.clear();
    });

    test("a hero htmx swaps in takes the framing saved for it", () => {
        localStorage.setItem(KEY, JSON.stringify({ zoom: "180", x: 20, y: 75 }));
        document.body.innerHTML = HERO;
        hero().dispatchEvent(new CustomEvent("htmx:load", { bubbles: true }));
        expect([hero().style.backgroundSize, hero().style.backgroundPosition, zoom()?.value]).toEqual(["180% auto", "20% 75%", "180"]);
    });

    test("Adjust reveals the zoom, which is saved as it moves", () => {
        document.body.innerHTML = HERO;
        hero().dispatchEvent(new CustomEvent("htmx:load", { bubbles: true }));
        el("adjust").click();
        expect(hero().classList.contains("is-adjusting-cover")).toBe(true);
        expect(el("controls").hidden).toBe(false);
        const input = zoom();
        if (!input) throw new Error("no zoom");
        input.value = "250";
        input.dispatchEvent(new Event("input", { bubbles: true }));
        expect(hero().style.backgroundSize).toBe("250% auto");
        expect(JSON.parse(localStorage.getItem(KEY) ?? "{}")).toEqual({ zoom: "250" });
        el("adjust").click();
        expect(el("controls").hidden).toBe(true);
    });

    test("dragging while adjusting moves the focal point, and only then", () => {
        document.body.innerHTML = HERO;
        Object.defineProperty(hero(), "clientWidth", { value: 1000 });
        Object.defineProperty(hero(), "clientHeight", { value: 200 });
        hero().setPointerCapture = () => undefined;
        const drag = (dx: number, dy: number) => {
            hero().dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, clientX: 500, clientY: 100, pointerId: 1 }));
            hero().dispatchEvent(new PointerEvent("pointermove", { bubbles: true, clientX: 500 + dx, clientY: 100 + dy, pointerId: 1 }));
            hero().dispatchEvent(new PointerEvent("pointerup", { bubbles: true, pointerId: 1 }));
        };
        drag(100, 20);
        expect(localStorage.getItem(KEY)).toBeNull();
        el("adjust").click();
        drag(100, 20);
        expect(JSON.parse(localStorage.getItem(KEY) ?? "{}")).toEqual({ x: 60, y: 60 });
        expect(hero().style.backgroundPosition).toBe("60% 60%");
    });
});

describe("framing with a second pointer", () => {
    test("a stray second pointer does not move the focal point or stack listeners", () => {
        localStorage.clear();
        document.body.innerHTML = `
            <section class="ul-page-hero is-adjusting-cover" id="pin-detail-hero" data-cover-state-key="ul_cover_hero_pin_x">
                <button type="button" data-cover-adjust-toggle>Adjust</button>
            </section>`;
        const hero = el("pin-detail-hero");
        Object.defineProperty(hero, "clientWidth", { value: 1000 });
        Object.defineProperty(hero, "clientHeight", { value: 200 });
        hero.setPointerCapture = () => undefined;
        const fire = (type: string, x: number, y: number, pointerId: number) => hero.dispatchEvent(new PointerEvent(type, { bubbles: true, clientX: x, clientY: y, pointerId }));
        fire("pointerdown", 500, 100, 1);
        fire("pointerdown", 100, 100, 2);
        fire("pointermove", 900, 100, 2);
        expect(localStorage.getItem("ul_cover_hero_pin_x")).toBeNull();
        fire("pointermove", 600, 100, 1);
        expect(JSON.parse(localStorage.getItem("ul_cover_hero_pin_x") ?? "{}")).toEqual({ x: 60, y: 50 });
        fire("pointerup", 600, 100, 1);
        fire("pointermove", 900, 100, 1);
        expect(JSON.parse(localStorage.getItem("ul_cover_hero_pin_x") ?? "{}")).toEqual({ x: 60, y: 50 });
    });

    test("a drag the browser cancels ends, so the next one still works", () => {
        localStorage.clear();
        document.body.innerHTML = `<section class="ul-page-hero is-adjusting-cover" id="pin-detail-hero" data-cover-state-key="ul_cover_hero_pin_y"></section>`;
        const hero = el("pin-detail-hero");
        Object.defineProperty(hero, "clientWidth", { value: 1000 });
        Object.defineProperty(hero, "clientHeight", { value: 200 });
        hero.setPointerCapture = () => undefined;
        const fire = (type: string, x: number, pointerId: number) => hero.dispatchEvent(new PointerEvent(type, { bubbles: true, clientX: x, clientY: 100, pointerId }));
        fire("pointerdown", 500, 3);
        fire("pointercancel", 500, 3);
        fire("pointermove", 900, 3);
        expect(localStorage.getItem("ul_cover_hero_pin_y")).toBeNull();
        fire("pointerdown", 500, 4);
        fire("pointermove", 400, 4);
        expect(JSON.parse(localStorage.getItem("ul_cover_hero_pin_y") ?? "{}")).toEqual({ x: 40, y: 50 });
        fire("pointerup", 400, 4);
    });
});
