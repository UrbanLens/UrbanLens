import { afterEach, beforeAll, beforeEach, expect, test } from "bun:test";

import { installCustomFieldForms, installFixedFieldDrag } from "./custom-field-forms";

const realFetch = globalThis.fetch;
const realToastr = window.toastr;
let posts: { url: string; body: string; reported: unknown }[];
let respond: () => Response | Promise<Response>;
let toasts: string[];

const STYLES = `<script type="application/json" id="cf-styles-data">{"number": [["input", "Input"], ["slider", "Slider"]], "select": [["dropdown", "Dropdown"]], "text": []}</script>`;
const FORM = (type: string, current = "") => `
  <form class="cf-def-form">
    <select class="cf-type-select">
      <option value="">Type...</option>
      <option value="text"${type === "text" ? " selected" : ""}>Text</option>
      <option value="number"${type === "number" ? " selected" : ""}>Number</option>
      <option value="select"${type === "select" ? " selected" : ""}>Select</option>
      <option value="reference"${type === "reference" ? " selected" : ""}>Reference</option>
    </select>
    <select class="cf-style-select" data-current="${current}" hidden></select>
    <select class="cf-ref-kind-select" hidden><option value="pin">Pin</option></select>
    <textarea class="cf-options-input" hidden></textarea>
    <span class="cf-slider-bounds" hidden></span>
  </form>`;

const $ = <T extends Element = HTMLElement>(sel: string): T | null => document.querySelector<T>(sel);
/** happy-dom misreads `selected` on selects parsed from innerHTML; a browser reads the attribute. */
const render = (html: string) => {
    document.body.innerHTML = html;
    for (const select of document.querySelectorAll<HTMLSelectElement>("select")) {
        const chosen = Array.from(select.options).find((option) => option.hasAttribute("selected"));
        if (chosen) select.value = chosen.value;
    }
};
const choose = (sel: string, value: string) => {
    const select = $<HTMLSelectElement>(sel);
    if (!select) throw new Error(`no ${sel}`);
    select.value = value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
};
const styleOptions = () => Array.from($<HTMLSelectElement>(".cf-style-select")?.options ?? []).map((o) => o.value);
const settle = async () => {
    for (let i = 0; i < 3; i++) await new Promise((resolve) => setTimeout(resolve, 0));
};

beforeAll(() => {
    installCustomFieldForms();
    installFixedFieldDrag();
});

beforeEach(() => {
    posts = [];
    toasts = [];
    respond = () => new Response("{}", { status: 200 });
    globalThis.fetch = Object.assign(
        async (input: RequestInfo | URL, init?: RequestInit) => {
            posts.push({ url: String(input), body: String(init?.body ?? ""), reported: init ? Reflect.get(init, "__ulReported") : undefined });
            return respond();
        },
        { preconnect: realFetch.preconnect },
    );
    window.toastr = Object.assign(Object.create(null), {
        success: (m: string) => toasts.push(`success:${m}`),
        error: (m: string) => toasts.push(`error:${m}`),
        info: (m: string) => toasts.push(`info:${m}`),
        warning: (m: string) => toasts.push(`warning:${m}`),
        clear: () => undefined,
    });
});

afterEach(() => {
    globalThis.fetch = realFetch;
    window.toastr = realToastr;
});

test("a form swapped in by htmx offers its type's styles, keeping the saved one", () => {
    render(STYLES + `<div id="panel">${FORM("number", "slider")}</div>`);
    document.getElementById("panel")?.dispatchEvent(new CustomEvent("htmx:load", { bubbles: true }));
    expect(styleOptions()).toEqual(["input", "slider"]);
    expect($<HTMLSelectElement>(".cf-style-select")?.value).toBe("slider");
    expect($(".cf-style-select")?.hidden).toBe(false);
    expect($(".cf-slider-bounds")?.hidden).toBe(false);
});

test("changing the type rebuilds the styles and shows only that type's extra inputs", () => {
    render(STYLES + FORM(""));
    document.body.dispatchEvent(new CustomEvent("htmx:load", { bubbles: true }));
    expect($(".cf-style-select")?.hidden).toBe(true);

    choose(".cf-type-select", "select");
    expect(styleOptions()).toEqual(["dropdown"]);
    expect([$(".cf-options-input")?.hidden, $<HTMLTextAreaElement>(".cf-options-input")?.required]).toEqual([false, true]);

    choose(".cf-type-select", "reference");
    expect($(".cf-style-select")?.hidden).toBe(true);
    expect([$(".cf-ref-kind-select")?.hidden, $<HTMLSelectElement>(".cf-ref-kind-select")?.required]).toEqual([false, true]);
    expect([$(".cf-options-input")?.hidden, $<HTMLTextAreaElement>(".cf-options-input")?.required]).toEqual([true, false]);

    choose(".cf-type-select", "number");
    expect($(".cf-slider-bounds")?.hidden).toBe(true);
    choose(".cf-style-select", "slider");
    expect($(".cf-slider-bounds")?.hidden).toBe(false);
});

test("dropping a fixed field saves where it landed; a failed save says so once", async () => {
    document.body.innerHTML = `
      <div class="cf-fixed-item" data-cf-position-url="/cf/7/position/" style="left: 10%; top: 20%">
        <div class="cf-fixed-handle">drag</div>
      </div>`;
    const handle = $(".cf-fixed-handle");
    const item = $(".cf-fixed-item");
    handle?.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, cancelable: true, clientX: 100, clientY: 100 }));
    expect(item?.classList.contains("is-dragging")).toBe(true);
    document.dispatchEvent(new PointerEvent("pointermove", { bubbles: true, clientX: 100 + window.innerWidth * 0.3, clientY: 100 + window.innerHeight * 0.1 }));
    document.dispatchEvent(new PointerEvent("pointerup", { bubbles: true }));
    await settle();
    expect(item?.classList.contains("is-dragging")).toBe(false);
    expect(posts).toHaveLength(1);
    expect(posts[0]?.url).toBe("/cf/7/position/");
    expect(posts[0]?.reported).toBe(true);
    const saved: unknown = JSON.parse(posts[0]?.body ?? "{}");
    expect(saved && typeof saved === "object" ? Object.keys(saved).sort() : []).toEqual(["left", "top"]);

    respond = () => new Response("", { status: 500 });
    handle?.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, cancelable: true, clientX: 50, clientY: 50 }));
    document.dispatchEvent(new PointerEvent("pointerup", { bubbles: true }));
    await settle();
    expect(toasts).toEqual(["error:Failed to save the field position."]);
});
