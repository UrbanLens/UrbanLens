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
