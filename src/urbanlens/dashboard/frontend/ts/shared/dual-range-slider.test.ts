import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { installGlobalDualRangeSlider, rangeLabel } from "./dual-range-slider";

function slider(id: string, extra = "", min = 0, max = 5, lo = min, hi = max): string {
    return `<div id="${id}" data-ul-dual-range-slider data-format="stars" data-accordion="acc" ${extra}>
      <input type="range" data-role="min" min="${min}" max="${max}" value="${lo}">
      <input type="range" data-role="max" min="${min}" max="${max}" value="${hi}">
      <span data-role="fill"></span><span data-role="value-label"></span>
      <input type="hidden" data-role="min-hidden"><input type="hidden" data-role="max-hidden">
    </div>`;
}

const q = <T extends Element>(selector: string): T => {
    const el = document.querySelector<T>(selector);
    if (!el) throw new Error(`no ${selector}`);
    return el;
};

function move(id: string, role: "min" | "max", value: number): void {
    const input = q<HTMLInputElement>(`#${id} [data-role="${role}"]`);
    input.value = String(value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
}

const realHtmx = window.htmx;

beforeAll(() => installGlobalDualRangeSlider());

afterAll(() => {
    window.htmx = realHtmx;
});

beforeEach(() => {
    document.body.innerHTML = `<details id="acc">${slider("rating")}${slider("score", "", 0, 10)}</details>`;
    window.UrbanLensDualRangeSlider?.initAll();
});

describe("rangeLabel", () => {
    test("reads the range the way the sidebar says it", () => {
        expect(rangeLabel("stars", 0, 5, 0, 5)).toBe("Any");
        expect(rangeLabel("stars", 0, 3, 0, 5)).toBe("up to ★★★☆☆");
        expect(rangeLabel("default", 4, 10, 0, 10)).toBe("4 or higher");
        expect(rangeLabel("default", 2, 7, 0, 10)).toBe("2 - 7");
    });
});

describe("sliders", () => {
    test("an untouched slider filters nothing", () => {
        expect(q<HTMLInputElement>('#rating [data-role="min-hidden"]').value).toBe("");
        expect(q<HTMLInputElement>('#rating [data-role="max-hidden"]').value).toBe("");
        expect(q("#rating [data-role=value-label]").textContent).toBe("Any");
        expect(q("#acc").classList.contains("fp-acc-active")).toBe(false);
    });

    test("narrowing one carries the bounds and marks its accordion", () => {
        move("rating", "min", 3);
        expect(q<HTMLInputElement>('#rating [data-role="min-hidden"]').value).toBe("3");
        expect(q<HTMLInputElement>('#rating [data-role="max-hidden"]').value).toBe("");
        expect(q<HTMLElement>("#rating [data-role=fill]").style.left).toBe("60%");
        expect(q("#acc").classList.contains("fp-acc-active")).toBe(true);
    });

    test("the thumbs cannot cross", () => {
        move("score", "max", 4);
        move("score", "min", 8);
        expect(q<HTMLInputElement>('#score [data-role="min"]').value).toBe("8");
        expect(q<HTMLInputElement>('#score [data-role="max"]').value).toBe("8");
    });

    test("reset returns every slider to its default", () => {
        move("rating", "min", 2);
        window.UrbanLensDualRangeSlider?.resetAll();
        expect(q<HTMLInputElement>('#rating [data-role="min"]').value).toBe("0");
        expect(q("#acc").classList.contains("fp-acc-active")).toBe(false);
    });

    test("a slider an htmx swap brings in is set up", () => {
        const host = document.createElement("div");
        host.innerHTML = slider("late", "", 0, 5, 2, 5);
        document.body.appendChild(host);
        host.dispatchEvent(new CustomEvent("htmx:afterSwap", { bubbles: true, detail: { target: host } }));
        expect(q("#late [data-role=value-label]").textContent).toBe("★★☆☆☆ or higher");
    });
});

/**
 * A slider inside its ``data-htmx-form`` must not re-trigger it: the native change already bubbles to the form,
 * and a second trigger makes ``hx-sync="this:replace"`` abort the first request, which htmx logs as an error.
 */
describe("the form a slider submits", () => {
    const SLIDER = `
        <div data-ul-dual-range-slider data-htmx-form="filter-form">
            <input type="hidden" name="min_priority" data-role="min-hidden" value="">
            <input type="hidden" name="max_priority" data-role="max-hidden" value="">
            <div data-role="fill"></div>
            <input type="range" data-role="min" min="0" max="5" value="0">
            <input type="range" data-role="max" min="0" max="5" value="5">
            <div data-role="value-label"></div>
        </div>`;
    let triggers: string[] = [];

    function mount(html: string): HTMLElement {
        triggers = [];
        document.body.innerHTML = html;
        window.htmx = {
            process: () => undefined,
            ajax: async () => undefined,
            trigger: (target, event) => {
                triggers.push(target.id);
                target.dispatchEvent(new Event(event, { bubbles: true }));
            },
        };
        window.UrbanLensDualRangeSlider?.initAll();
        return q<HTMLElement>("#filter-form");
    }

    function releaseMinThumb(value: string): void {
        const min = q<HTMLInputElement>('[data-role="min"]');
        min.value = value;
        min.dispatchEvent(new Event("input", { bubbles: true }));
        min.dispatchEvent(new Event("change", { bubbles: true }));
    }

    test("inside its form, it submits once per change", () => {
        let changes = 0;
        mount(`<form id="filter-form">${SLIDER}</form>`).addEventListener("change", () => (changes += 1));
        releaseMinThumb("5");
        expect(q<HTMLInputElement>('[name="min_priority"]').value).toBe("5");
        expect(changes).toBe(1);
        expect(triggers).toEqual([]);
    });

    test("outside its form, it triggers the form itself", () => {
        let changes = 0;
        mount(`<form id="filter-form"></form>${SLIDER}`).addEventListener("change", () => (changes += 1));
        releaseMinThumb("3");
        expect(changes).toBe(1);
        expect(triggers).toEqual(["filter-form"]);
    });
});
