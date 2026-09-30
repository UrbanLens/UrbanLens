import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { replaceContactPicker, SafetyAutosave, type SafetySaveResponse } from "./safety-autosave";

const realFetch = globalThis.fetch;
const realToastr = window.toastr;
let posts: Record<string, FormDataEntryValue[]>[] = [];
let respond: () => Response = () => new Response(JSON.stringify({}), { status: 200 });
let errors: string[] = [];
let saved: SafetySaveResponse[] = [];

const settle = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

function picker(chips: string): string {
    return `<div class="safety-contact-picker" data-role="contact-picker">
      <button type="button" data-role="edit-toggle"><i class="material-symbols-outlined">add</i></button>
      <div data-role="chips">${chips}</div>
      <input type="text" data-role="input">
      <div data-role="suggestions" hidden></div>
    </div>`;
}

function render(floating = true): SafetyAutosave {
    document.body.innerHTML = `
      <p id="status" class="is-hidden"></p>
      <form id="f" action="/safety/c/abc/">
        <input name="title" value="Night walk">
        <input type="range" name="grace_period_hours" value="2">
        <input type="hidden" name="destination_lat" value="">
        <div id="picker-box">${picker("")}</div>
      </form>`;
    const form = document.getElementById("f");
    if (!(form instanceof HTMLFormElement)) throw new Error("no form");
    const autosave = new SafetyAutosave({
        form,
        status: document.getElementById("status"),
        floating,
        failureMessage: "Could not save.",
        onSaved: (data) => void saved.push(data),
    });
    autosave.install();
    return autosave;
}

function fire(selector: string, type: string): void {
    document.querySelector(selector)?.dispatchEvent(new Event(type, { bubbles: true }));
}

beforeAll(() => {
    globalThis.fetch = Object.assign(async (_input: RequestInfo | URL, init?: RequestInit) => {
        const body = init?.body;
        if (body instanceof FormData) {
            const entries: Record<string, FormDataEntryValue[]> = {};
            for (const [k, v] of body) (entries[k] ??= []).push(v);
            posts.push(entries);
        }
        return respond();
    }, realFetch);
});

afterAll(() => {
    globalThis.fetch = realFetch;
    window.toastr = realToastr;
});

beforeEach(() => {
    posts = [];
    errors = [];
    saved = [];
    respond = () => new Response(JSON.stringify({}), { status: 200 });
    window.toastr = { success: () => undefined, error: (m) => void errors.push(m), warning: () => undefined, info: () => undefined, clear: () => undefined };
});

describe("when it saves", () => {
    test("typing saves once, after a pause", async () => {
        render();
        fire('[name="title"]', "input");
        fire('[name="title"]', "input");
        await settle(10);
        expect(posts).toEqual([]);
        await settle(850);
        expect(posts.length).toBe(1);
        expect(posts[0]?.title).toEqual(["Night walk"]);
    });

    test("a slider or a map-set field saves at once; dragging a slider does not", async () => {
        render();
        fire('[name="grace_period_hours"]', "input");
        await settle();
        expect(posts).toEqual([]);
        fire('[name="grace_period_hours"]', "change");
        fire('[name="destination_lat"]', "change");
        await settle();
        expect(posts.length).toBe(2);
    });

    test("typing in the contact search box saves nothing; a chip does", async () => {
        render();
        fire('[data-role="input"]', "input");
        await settle(850);
        expect(posts).toEqual([]);
        document.querySelector('[data-role="contact-picker"]')?.dispatchEvent(new CustomEvent("contactschange", { bubbles: true }));
        await settle();
        expect(posts.length).toBe(1);
    });
});

describe("the outcome", () => {
    test("a save hands its response on and says so", async () => {
        respond = () => new Response(JSON.stringify({ title: "Night walk", warnings: [] }), { status: 200 });
        render().schedule(true);
        await settle();
        expect(saved).toEqual([{ title: "Night walk", warnings: [] }]);
        const status = document.getElementById("status");
        expect(status?.textContent).toBe("Saved");
        expect(status?.classList.contains("is-hidden")).toBe(false);
    });

    test("a failure says so, and stays up", async () => {
        respond = () => new Response("<h1>oops</h1>", { status: 500 });
        render().schedule(true);
        await settle(1700);
        const status = document.getElementById("status");
        expect(status?.textContent).toBe("Could not save");
        expect(status?.classList.contains("safety-autosave-status--error")).toBe(true);
        expect(status?.classList.contains("is-hidden")).toBe(false);
        expect(errors).toEqual(["Could not save."]);
        expect(saved).toEqual([]);
    });
});

describe("replaceContactPicker", () => {
    test("the fresh picker works and stays open if it was being edited", () => {
        render();
        const box = document.getElementById("picker-box");
        if (!box) throw new Error("no box");
        box.querySelector(".safety-contact-picker")?.classList.add("is-editing");
        replaceContactPicker(box, picker('<span class="safety-contact-chip" data-type="email" data-email="a@b.co"><input type="hidden" name="contact_emails" value="a@b.co"><button type="button" data-role="remove">x</button></span>'));
        const fresh = box.querySelector(".safety-contact-picker");
        expect(fresh?.classList.contains("is-editing")).toBe(true);
        expect(box.querySelector('[data-role="edit-toggle"]')?.getAttribute("aria-label")).toBe("Done editing emergency contacts");
        box.querySelector<HTMLElement>('[data-role="remove"]')?.click();
        expect(box.querySelector(".safety-contact-chip")).toBeNull();
    });
});
