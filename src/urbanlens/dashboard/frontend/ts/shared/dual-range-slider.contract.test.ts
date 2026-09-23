/**
 * The dual-range slider's inline script (`_dual_range_slider_script.html`) and the form it submits.
 *
 * A slider inside its `data-htmx-form` must not re-trigger it: the native `change` already bubbles to
 * the form, and a second trigger makes `hx-sync="this:replace"` abort the first request, which htmx
 * logs as a console error.
 */

import { afterEach, beforeAll, describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const TEMPLATE = join(import.meta.dir, "../../../templates/dashboard/partials/ui/_dual_range_slider_script.html");

const scriptBody = /<script>([\s\S]*)<\/script>/.exec(readFileSync(TEMPLATE, "utf8"))?.[1] ?? "";

const SLIDER = `
    <div data-ul-dual-range-slider data-htmx-form="filter-form">
        <input type="hidden" name="min_priority" data-role="min-hidden" value="">
        <input type="hidden" name="max_priority" data-role="max-hidden" value="">
        <div data-role="fill"></div>
        <input type="range" data-role="min" min="0" max="5" value="0">
        <input type="range" data-role="max" min="0" max="5" value="5">
        <div data-role="value-label"></div>
    </div>`;

let htmxTriggers: Array<{ target: Element; event: string }> = [];

beforeAll(() => {
    expect(scriptBody).toContain("data-htmx-form");
});

afterEach(() => {
    document.body.innerHTML = "";
    htmxTriggers = [];
});

/** Renders `html`, stubs htmx to record triggers, and runs the slider script over it. */
function mount(html: string): void {
    document.body.innerHTML = html;
    (window as unknown as { htmx: unknown }).htmx = {
        trigger(target: Element, event: string) {
            htmxTriggers.push({ target, event });
            target.dispatchEvent(new Event(event, { bubbles: true }));
        },
    };
    new Function(scriptBody)();
}

function releaseMinThumb(value: string): void {
    const min = document.querySelector<HTMLInputElement>('[data-role="min"]')!;
    min.value = value;
    min.dispatchEvent(new Event("input", { bubbles: true }));
    min.dispatchEvent(new Event("change", { bubbles: true }));
}

describe("dual-range slider submission", () => {
    test("a slider inside its form submits it once per change", () => {
        mount(`<form id="filter-form">${SLIDER}</form>`);
        let formChanges = 0;
        document.getElementById("filter-form")!.addEventListener("change", () => {
            formChanges += 1;
        });

        releaseMinThumb("5");

        expect(document.querySelector<HTMLInputElement>('[name="min_priority"]')!.value).toBe("5");
        expect(formChanges).toBe(1);
        expect(htmxTriggers).toHaveLength(0);
    });

    test("a slider outside its form still triggers it", () => {
        mount(`<form id="filter-form"></form>${SLIDER}`);
        let formChanges = 0;
        document.getElementById("filter-form")!.addEventListener("change", () => {
            formChanges += 1;
        });

        releaseMinThumb("3");

        expect(formChanges).toBe(1);
        expect(htmxTriggers.map((t) => t.target.id)).toEqual(["filter-form"]);
    });
});
