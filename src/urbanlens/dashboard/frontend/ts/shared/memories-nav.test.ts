import { beforeAll, beforeEach, expect, test } from "bun:test";

import { installMemoriesNav } from "./memories-nav";

beforeAll(() => {
    installMemoriesNav();
    installMemoriesNav();
});

beforeEach(() => {
    localStorage.removeItem("ul_pins_dirty");
    document.body.innerHTML = `
      <nav data-memories-nav>
        <a id="memories-visits-tab">Visits <span id="memories-visits-tab-badge">3</span></a>
      </nav>
      <form id="f"></form>`;
});

const after = (detail: Record<string, unknown>) => document.getElementById("f")?.dispatchEvent(new CustomEvent("htmx:afterRequest", { bubbles: true, detail }));
const xhr = {};

test("a successful write on a Memories page tells the map its pins changed", () => {
    after({ xhr, successful: true, requestConfig: { verb: "get" } });
    expect(localStorage.getItem("ul_pins_dirty")).toBeNull();
    after({ xhr, successful: false, requestConfig: { verb: "post" } });
    expect(localStorage.getItem("ul_pins_dirty")).toBeNull();
    after({ xhr, successful: true, requestConfig: { verb: "post" } });
    expect(localStorage.getItem("ul_pins_dirty")).toBe("1");
});

test("off the Memories pages a write leaves the flag alone", () => {
    document.body.innerHTML = `<form id="f"></form>`;
    after({ xhr, successful: true, requestConfig: { verb: "post" } });
    expect(localStorage.getItem("ul_pins_dirty")).toBeNull();
});

test("the Visits tab follows the unlogged count down, and goes at zero", () => {
    const changed = (count: unknown) => document.body.dispatchEvent(new CustomEvent("unloggedVisitsCountChanged", { bubbles: true, detail: { count } }));
    changed("2");
    expect(document.getElementById("memories-visits-tab-badge")?.textContent).toBe("3");
    changed(2);
    expect(document.getElementById("memories-visits-tab-badge")?.textContent).toBe("2");
    changed(0);
    expect(document.getElementById("memories-visits-tab")).toBeNull();
});
