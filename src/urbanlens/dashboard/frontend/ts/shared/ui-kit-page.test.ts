import { afterAll, beforeEach, expect, test } from "bun:test";

import { installUiKitPage } from "./ui-kit-page";

const realToastr = window.toastr;
let toasts: string[] = [];
let uninstall: () => void = () => {};

beforeEach(() => {
    uninstall();
    toasts = [];
    window.toastr = {
        success: (m: string) => void toasts.push(`success:${m}`),
        error: (m: string) => void toasts.push(`error:${m}`),
        info: (m: string) => void toasts.push(`info:${m}`),
        warning: (m: string) => void toasts.push(`warning:${m}`),
        clear: () => undefined,
    };
    document.body.innerHTML = `
      <div class="ui-kit-page">
        <div class="ul-tab-bar"><button class="ul-tab ul-tab--active" data-ui-kit-tab="1">A</button><button class="ul-tab" data-ui-kit-tab="2"><i>b</i></button></div>
        <label class="ul-radio-button active" id="light"><input type="radio" name="r" checked>Light</label>
        <label class="ul-radio-button" id="dark"><input type="radio" name="r">Dark</label>
        <div class="ui-kit-demo-dropdown" id="ui-kit-dropdown"><button id="ui-kit-dropdown-toggle"><i>more</i></button><div class="ul-dropdown"><button id="item">Edit</button></div></div>
        <button data-toast="warning">Warn</button><button data-toast="bogus">Bogus</button>
        <a href="#" data-demo-link id="demo">Manage all</a>
        <p id="outside">elsewhere</p>
      </div>`;
    uninstall = installUiKitPage(document);
});

afterAll(() => {
    uninstall();
    window.toastr = realToastr;
});

const $ = (sel: string): HTMLElement => {
    const el = document.querySelector<HTMLElement>(sel);
    if (!el) throw new Error(sel);
    return el;
};
const click = (sel: string): MouseEvent => {
    const event = new MouseEvent("click", { bubbles: true, cancelable: true });
    $(sel).dispatchEvent(event);
    return event;
};

test("a tab becomes the active one in its bar", () => {
    click('[data-ui-kit-tab="2"] i');
    expect(Array.from(document.querySelectorAll(".ul-tab")).map((t) => t.classList.contains("ul-tab--active"))).toEqual([false, true]);
});

test("the checked radio's label is the active one", () => {
    const dark = $("#dark input");
    if (!(dark instanceof HTMLInputElement)) throw new Error("radio");
    dark.checked = true;
    dark.dispatchEvent(new Event("change", { bubbles: true }));
    expect([$("#light").classList.contains("active"), $("#dark").classList.contains("active")]).toEqual([false, true]);
});

test("the menu opens from its toggle, stays open for its own items, and closes on a click elsewhere", () => {
    click("#ui-kit-dropdown-toggle i");
    expect($("#ui-kit-dropdown").classList.contains("is-open")).toBe(true);
    click("#item");
    expect($("#ui-kit-dropdown").classList.contains("is-open")).toBe(true);
    click("#outside");
    expect($("#ui-kit-dropdown").classList.contains("is-open")).toBe(false);
    click("#ui-kit-dropdown-toggle");
    click("#ui-kit-dropdown-toggle");
    expect($("#ui-kit-dropdown").classList.contains("is-open")).toBe(false);
});

test("toast buttons show their kind of toast; demo links go nowhere", () => {
    click('[data-toast="warning"]');
    click('[data-toast="bogus"]');
    expect(toasts).toEqual(["warning:Please review before continuing."]);
    expect(click("#demo").defaultPrevented).toBe(true);
});
