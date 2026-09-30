import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { installApiLimitsPage } from "./api-limits-page";

const CARD = (service: string, category: string, enabled: boolean) => `
  <div class="api-limit-card" data-service="${service}" data-category="${category}" data-search-text="${service} maps">
    <form class="api-limit-form">
      <span class="api-limit-status"></span>
      <i class="api-state-icon">${enabled ? "cloud" : "cloud_off"}</i>
      <input type="checkbox" name="enabled" class="api-enabled-cb" ${enabled ? "checked" : ""}>
      <input type="text" name="notes">
    </form>
  </div>`;

let uninstall: () => void = () => {};

beforeAll(() => {
    document.body.innerHTML = "";
});

beforeEach(() => {
    uninstall();
    document.body.innerHTML = `
      <input id="api-limits-search-input">
      <button class="api-limits-tab is-active" data-category="" aria-selected="true">All</button>
      <button class="api-limits-tab" data-category="Geocoding" aria-selected="false">Geocoding</button>
      ${CARD("nominatim", "Geocoding", true)}${CARD("flickr", "Photos", false)}`;
    uninstall = installApiLimitsPage(document);
});

afterAll(() => uninstall());

function card(service: string): HTMLElement {
    const el = document.querySelector<HTMLElement>(`.api-limit-card[data-service="${service}"]`);
    if (!el) throw new Error(service);
    return el;
}

function checkbox(service: string): HTMLInputElement {
    const el = card(service).querySelector(".api-enabled-cb");
    if (!(el instanceof HTMLInputElement)) throw new Error(service);
    return el;
}

function htmx(service: string, name: string, status?: number): void {
    const form = card(service).querySelector("form");
    form?.dispatchEvent(new CustomEvent(name, { bubbles: true, detail: { elt: form, xhr: { status } } }));
}

function status(service: string): string {
    const el = card(service).querySelector(".api-limit-status");
    return `${el?.className}|${el?.textContent}`;
}

describe("the enable toggle", () => {
    test("shows the new state straight away", () => {
        const cb = checkbox("flickr");
        cb.checked = true;
        cb.dispatchEvent(new Event("change", { bubbles: true }));
        expect(card("flickr").classList.contains("is-disabled")).toBe(false);
        expect(card("flickr").querySelector(".api-state-icon")?.textContent).toBe("cloud");
    });

    test("a failed save goes back to what the server last confirmed", () => {
        const cb = checkbox("nominatim");
        cb.checked = false;
        cb.dispatchEvent(new Event("change", { bubbles: true }));
        htmx("nominatim", "htmx:beforeRequest");
        expect(status("nominatim")).toBe("api-limit-status is-saving|Saving...");
        htmx("nominatim", "htmx:afterRequest", 500);
        expect(cb.checked).toBe(true);
        expect(card("nominatim").classList.contains("is-disabled")).toBe(false);
        expect(status("nominatim")).toBe("api-limit-status is-error|Save failed (500)");
    });

    test("a saved change becomes the one a later failure returns to", () => {
        const cb = checkbox("nominatim");
        cb.checked = false;
        cb.dispatchEvent(new Event("change", { bubbles: true }));
        htmx("nominatim", "htmx:afterRequest", 200);
        document.body.dispatchEvent(new CustomEvent("apiLimitSaved", { detail: { service: "nominatim" } }));
        expect(status("nominatim")).toBe("api-limit-status is-saved|Saved");
        htmx("nominatim", "htmx:afterRequest", 0);
        expect(cb.checked).toBe(false);
        expect(status("nominatim")).toBe("api-limit-status is-error|Save failed (?)");
    });
});

describe("filtering", () => {
    test("search and tab narrow the cards together", () => {
        const search = document.getElementById("api-limits-search-input");
        if (!(search instanceof HTMLInputElement)) throw new Error("search");
        document.querySelector<HTMLElement>('.api-limits-tab[data-category="Geocoding"]')?.click();
        expect([card("nominatim").hidden, card("flickr").hidden]).toEqual([false, true]);
        expect(document.querySelector('.api-limits-tab[data-category="Geocoding"]')?.getAttribute("aria-selected")).toBe("true");
        search.value = "flickr";
        search.dispatchEvent(new Event("input", { bubbles: true }));
        expect([card("nominatim").hidden, card("flickr").hidden]).toEqual([true, true]);
        document.querySelector<HTMLElement>('.api-limits-tab[data-category=""]')?.click();
        expect([card("nominatim").hidden, card("flickr").hidden]).toEqual([true, false]);
    });
});
