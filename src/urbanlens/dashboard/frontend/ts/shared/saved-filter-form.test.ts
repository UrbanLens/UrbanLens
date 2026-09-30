import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { installSavedFilterForm } from "./saved-filter-form";
import { matchLabel } from "./saved-filter-preview";

interface Call {
    url: string;
    method: string;
}

const realFetch = globalThis.fetch;
const realConfirm = window.confirm;
const realToastr = window.toastr;
let calls: Call[] = [];
let respond: (url: string) => Response = () => new Response("", { status: 200 });
let errors: string[] = [];
let answer = true;

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

function render(): void {
    document.body.innerHTML = `
      <div id="saved-filter-config" data-delete-url-template="/filters/00000000-0000-0000-0000-000000000000/delete/"
           data-region-search-url="/regions/" data-suggest-name-url="/filters/suggest/"></div>
      <input type="search" id="saved-filters-search">
      <div id="saved-filters-grid">
        <a class="saved-filter-card" data-search="ruins" href="/filters/a/">
          <button type="button" data-saved-filter-action="delete" data-filter-uuid="aaaa" data-filter-name="Ruins">Delete</button>
        </a>
        <a class="saved-filter-card" data-search="tunnels" href="/filters/b/"></a>
      </div>
      <form id="saved-filter-form" data-action="/filters/new/">
        <input id="saved-filter-name" name="name" value="">
        <input type="hidden" name="has_visits" id="sf-visits-hidden" value="yes">
        <button type="button" id="sf-visits-reset" data-saved-filter-action="reset-visits">x</button>
        <div id="sf-visits-dates"><input type="date" value="2026-01-01"></div>
        <button type="button" class="saved-filter-region-mode-btn saved-filter-region-mode-btn--active" data-region-mode="include">In</button>
        <button type="button" class="saved-filter-region-mode-btn" data-region-mode="exclude"><i>Out</i></button>
        <input id="saved-filter-region-search-input" value="Albany">
        <button type="button" data-saved-filter-action="search-region">Search</button>
        <div id="saved-filter-region-search-results" hidden></div>
        <p id="saved-filter-region-search-message" hidden></p>
        <input type="hidden" name="include_regions" id="saved-filter-include-regions" value="">
        <input type="hidden" name="exclude_regions" id="saved-filter-exclude-regions" value="">
        <button type="submit">Save</button>
      </form>`;
}

beforeAll(() => {
    installSavedFilterForm();
    installSavedFilterForm();
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        calls.push({ url, method: init?.method ?? "GET" });
        return respond(url);
    }, realFetch);
    window.confirm = () => answer;
});

afterAll(() => {
    globalThis.fetch = realFetch;
    window.confirm = realConfirm;
    window.toastr = realToastr;
});

beforeEach(() => {
    calls = [];
    errors = [];
    answer = true;
    respond = () => new Response("", { status: 200 });
    window.toastr = { success: () => undefined, error: (m) => void errors.push(m), warning: () => undefined, info: () => undefined, clear: () => undefined };
    render();
});

describe("save", () => {
    test("a refused save keeps the form and gives the server's reason", async () => {
        respond = () => new Response(JSON.stringify({ error: "A filter with that name already exists." }), { status: 400 });
        document.getElementById("saved-filter-form")?.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
        await settle();
        expect(calls).toEqual([{ url: "/filters/new/", method: "POST" }]);
        expect(errors).toEqual(["A filter with that name already exists."]);
    });
});

describe("delete", () => {
    test("inside a filter card, it deletes without following the card's link", async () => {
        respond = () => new Response("", { status: 500 });
        const click = new MouseEvent("click", { bubbles: true, cancelable: true });
        document.querySelector('[data-saved-filter-action="delete"]')?.dispatchEvent(click);
        await settle();
        expect(click.defaultPrevented).toBe(true);
        expect(calls).toEqual([{ url: "/filters/aaaa/delete/", method: "POST" }]);
        expect(errors).toEqual(["Could not delete this filter."]);
    });

    test("declining sends nothing", async () => {
        answer = false;
        document.querySelector<HTMLElement>('[data-saved-filter-action="delete"]')?.click();
        await settle();
        expect(calls).toEqual([]);
    });
});

describe("fields", () => {
    test("clearing Visited? clears its dates too", () => {
        document.querySelector<HTMLElement>('[data-saved-filter-action="reset-visits"]')?.click();
        expect(document.querySelector<HTMLInputElement>("#sf-visits-hidden")?.value).toBe("");
        expect(document.getElementById("sf-visits-dates")?.style.display).toBe("none");
        expect(document.querySelector<HTMLInputElement>("#sf-visits-dates input")?.value).toBe("");
    });

    test("the region mode buttons choose what gets drawn", () => {
        document.querySelector<HTMLElement>('[data-region-mode="exclude"] i')?.click();
        const active = Array.from(document.querySelectorAll<HTMLElement>(".saved-filter-region-mode-btn--active"), (b) => b.dataset.regionMode);
        expect(active).toEqual(["exclude"]);
    });

    test("a place search that finds nothing says so", async () => {
        respond = () => new Response(JSON.stringify({ results: [] }), { status: 200 });
        document.querySelector<HTMLElement>('[data-saved-filter-action="search-region"]')?.click();
        await settle();
        expect(calls[0]?.url).toBe("/regions/?q=Albany");
        const message = document.getElementById("saved-filter-region-search-message");
        expect(message?.hidden).toBe(false);
        expect(message?.textContent).toContain("No area boundary found");
    });

    test("Enter in the place box searches instead of submitting", async () => {
        respond = () => new Response("", { status: 503 });
        const key = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
        document.getElementById("saved-filter-region-search-input")?.dispatchEvent(key);
        await settle();
        expect(key.defaultPrevented).toBe(true);
        expect(document.getElementById("saved-filter-region-search-message")?.textContent).toBe("Could not search for that place right now.");
    });

    test("the Filters tab's search narrows the cards", () => {
        const search = document.getElementById("saved-filters-search");
        if (!(search instanceof HTMLInputElement)) throw new Error("no search box");
        search.value = "tun";
        search.dispatchEvent(new Event("input", { bubbles: true }));
        const shown = Array.from(document.querySelectorAll<HTMLElement>(".saved-filter-card"), (c) => c.style.display !== "none");
        expect(shown).toEqual([false, true]);
    });
});

describe("matchLabel", () => {
    test("says when the map shows only a sample", () => {
        expect(matchLabel(1, 1, "500")).toBe("1 matching pin");
        expect(matchLabel(900, 500, "500")).toBe("900 matching pins (showing 500 on the map, spread across the area)");
    });
});
