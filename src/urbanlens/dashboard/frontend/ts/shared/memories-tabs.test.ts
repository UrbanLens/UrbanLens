import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { installAcceptAll, installMapsTab, installPhotoPickCaptions, mapDeleteMessage, parseSuggestion, parseUnloggedPin, suggestionTooltip, unloggedBulkBody } from "./memories-tabs";
import { parseItemId, reportBulkOutcome } from "./pin-select-map";

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const realFetch = globalThis.fetch;
const realToastr = window.toastr;
const realConfirm = window.confirmDialog;
let toasts: string[] = [];
let requests: { url: string; method: string }[] = [];
let respond: () => Response = () => new Response(JSON.stringify({ processed: 2, requested: 2 }), { status: 200 });

beforeEach(() => {
    toasts = [];
    requests = [];
    respond = () => new Response(JSON.stringify({ processed: 2, requested: 2 }), { status: 200 });
    window.toastr = {
        success: (m: string) => void toasts.push(`success:${m}`),
        info: (m: string) => void toasts.push(`info:${m}`),
        warning: (m: string) => void toasts.push(`warning:${m}`),
        error: (m: string) => void toasts.push(`error:${m}`),
        clear: () => {},
    };
    globalThis.fetch = Object.assign(
        async (input: RequestInfo | URL, init?: RequestInit) => {
            requests.push({ url: String(input), method: init?.method ?? "GET" });
            return respond();
        },
        { preconnect: realFetch.preconnect },
    );
});

afterEach(() => {
    globalThis.fetch = realFetch;
    window.toastr = realToastr;
    window.confirmDialog = realConfirm;
});

describe("map data", () => {
    test("a suggestion needs an id and a position", () => {
        expect(parseSuggestion({ id: 4, latitude: 1.5, longitude: 2.5, name: "Mill", hit_count: 3, is_new_pin: true })).toEqual({ id: 4, latitude: 1.5, longitude: 2.5, name: "Mill", hit_count: 3, is_new_pin: true });
        expect(parseSuggestion({ id: 4, latitude: null, longitude: 2 })).toBeNull();
        expect(parseSuggestion("nope")).toBeNull();
    });

    test("an unlogged pin is keyed by slug", () => {
        expect(parseUnloggedPin({ id: "old-mill", latitude: 1, longitude: 2, name: null })?.id).toBe("old-mill");
        expect(parseUnloggedPin({ id: 7, latitude: 1, longitude: 2 })).toBeNull();
    });

    test("an unnamed suggestion is labelled by its coordinates", () => {
        expect(suggestionTooltip({ id: 1, latitude: 40.1234567, longitude: -74.5, name: "", hit_count: 1, is_new_pin: true })).toBe("40.12346, -74.50000 · 1 photo");
        expect(suggestionTooltip({ id: 1, latitude: 0, longitude: 0, name: "Mill", hit_count: 3, is_new_pin: true })).toBe("Mill · 3 photos");
    });

    test("a checkbox id matches the map data's type", () => {
        expect(parseItemId("12")).toBe(12);
        expect(parseItemId("old-mill")).toBe("old-mill");
        expect(parseItemId("")).toBeNull();
    });
});

describe("reportBulkOutcome", () => {
    test("says what happened to each row", () => {
        reportBulkOutcome({ processed: 3 }, "Accepted", "suggestion");
        reportBulkOutcome({ processed: 1, failed: 2, requested: 3 }, "Accepted", "suggestion");
        reportBulkOutcome({ processed: 0, failed: 1 }, "Logged", "visit");
        reportBulkOutcome({ processed: 1, skipped: 1 }, "Logged", "visit");
        reportBulkOutcome({ requested: 0 }, "Logged", "visit");
        expect(toasts).toEqual([
            "success:Accepted 3 suggestions.",
            "warning:Accepted 1 of 3 suggestions. 2 went wrong - please try again.",
            "error:Something went wrong with 1 visit. Please try again.",
            "warning:Logged 1 of 2 visits. The rest had already been handled.",
            "info:There was nothing to do.",
        ]);
    });
});

describe("accept all", () => {
    function button(onboarding: boolean): HTMLButtonElement {
        document.body.innerHTML = `<button id="b" data-url="/accept-all/" data-onboarding="${onboarding ? "1" : "0"}" data-done-url="/map/?suggestions_imported=1">Accept all</button>`;
        const el = document.getElementById("b");
        if (!(el instanceof HTMLButtonElement)) throw new Error("fixture");
        return el;
    }

    test("from onboarding, goes back to the map", async () => {
        const navigated: string[] = [];
        const el = button(true);
        installAcceptAll(el, (url) => void navigated.push(url));
        el.click();
        await settle();
        await settle();
        expect(requests).toEqual([{ url: "/accept-all/", method: "POST" }]);
        expect(toasts).toEqual(["success:Accepted 2 suggestions."]);
        expect(navigated).toEqual(["/map/?suggestions_imported=1"]);
    });

    test("a failure says so and lets it be tried again", async () => {
        respond = () => new Response("", { status: 500 });
        const el = button(false);
        installAcceptAll(el, () => {});
        el.click();
        await settle();
        await settle();
        expect(toasts).toEqual(["error:Something went wrong. Please try again."]);
        expect(el.disabled).toBe(false);
    });
});

test("photo picks keep their card's caption in step", () => {
    document.body.innerHTML = `<div id="wrap"><div class="pin-suggestion-card">
      <label class="photo-pick"><input type="checkbox" name="image_ids" value="1"></label>
      <label class="photo-pick"><input type="checkbox" name="asset_ids" value="a"></label>
      <p data-photo-caption></p></div></div>`;
    const wrap = document.getElementById("wrap");
    if (!wrap) throw new Error("fixture");
    installPhotoPickCaptions(wrap);
    const first = wrap.querySelector<HTMLInputElement>('input[name="image_ids"]');
    if (!first) throw new Error("fixture");
    first.checked = true;
    first.dispatchEvent(new Event("change", { bubbles: true }));
    expect(wrap.querySelector("[data-photo-caption]")?.textContent).toBe("1 of 2 selected for the pin's gallery");
    expect(first.closest(".photo-pick")?.classList.contains("photo-pick--selected")).toBe(true);
});

test("logging uses the bulk bar's date; unmarking does not", () => {
    document.body.innerHTML = `<div id="ul-bulk-bar-unlogged_visits"><input data-bulk-date value="2024-06-01"></div>`;
    expect(unloggedBulkBody("log", ["a", "b"])).toEqual({ pin_slugs: ["a", "b"], visited_date: "2024-06-01" });
    expect(unloggedBulkBody("unmark", ["a"])).toEqual({ pin_slugs: ["a"] });
});

describe("maps tab", () => {
    beforeEach(() => {
        document.body.innerHTML = `<div id="memories-maps-page" data-view-state-url-template="/markup-maps/00000000-0000-0000-0000-000000000000/view-state/">
          <div id="card-1"><button class="memories-map-delete-btn" data-delete-url="/markup-maps/1/delete/" data-card-id="card-1" data-attachment-labels="Trip: Coast||Check-in: Mill">Delete</button></div>
        </div>`;
    });

    test("deleting names what the map is attached to, and removes the card once confirmed", async () => {
        const asked: string[] = [];
        window.confirmDialog = async (options) => {
            asked.push(typeof options === "string" ? options : (options.message ?? ""));
            return true;
        };
        const page = document.getElementById("memories-maps-page");
        if (!page) throw new Error("fixture");
        installMapsTab(page);
        document.querySelector<HTMLElement>(".memories-map-delete-btn")?.click();
        await settle();
        await settle();
        expect(asked).toEqual([mapDeleteMessage(["Trip: Coast", "Check-in: Mill"])]);
        expect(asked[0]).toContain("• Trip: Coast\n• Check-in: Mill");
        expect(requests).toEqual([{ url: "/markup-maps/1/delete/", method: "POST" }]);
        expect(document.getElementById("card-1")).toBeNull();
    });

    test("declining deletes nothing", async () => {
        window.confirmDialog = async () => false;
        const page = document.getElementById("memories-maps-page");
        if (!page) throw new Error("fixture");
        installMapsTab(page);
        document.querySelector<HTMLElement>(".memories-map-delete-btn")?.click();
        await settle();
        expect(requests).toEqual([]);
        expect(document.getElementById("card-1")).not.toBeNull();
    });
});
