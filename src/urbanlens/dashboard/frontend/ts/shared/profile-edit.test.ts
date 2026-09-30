import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { ProfileEditForm } from "./profile-edit";

interface Call {
    url: string;
    body: Record<string, FormDataEntryValue>;
}

const realFetch = globalThis.fetch;
const realToastr = window.toastr;
const realShowModal = HTMLDialogElement.prototype.showModal;
let calls: Call[] = [];
let toasts: string[] = [];
let respond: () => Response = () => new Response(JSON.stringify({ ok: true }), { status: 200 });

const settle = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

function render(openOnLoad = false): void {
    document.body.innerHTML = `
      <div id="profile-hero-avatar" class="profile-avatar-placeholder">JM</div>
      <dialog id="date-error-dialog" ${openOnLoad ? "data-open-on-load" : ""}>
        <ul class="date-error-list"><li>Server-rendered error</li></ul>
      </dialog>
      <div class="edit-profile-page" data-save-url="/profile/field/" data-map-url="/map/">
        <button type="button" id="setup-skip-btn">Skip</button>
        <span data-field-status="username"></span>
        <input id="username-input" value="owl_one">
        <span id="username-hint"></span>
        <p id="username-requirements-hint" style="display:none"></p>
        <div class="edit-avatar-placeholder" id="avatar-preview">JM</div>
        <span data-field-status="avatar"></span>
        <button type="button" id="avatar-gravatar-btn">Gravatar</button>
        <span data-field-status="birth_date"></span>
        <input type="date" data-autosave="birth_date" value="">
        <span data-field-status="bio"></span>
        <textarea data-autosave="bio"></textarea>
        <select data-preference-toggle="contact_pref"><option value="dm">DM</option><option value="other">Other</option></select>
        <div data-preference-other-for="contact_pref" style="display:none"></div>
      </div>`;
    const root = document.querySelector<HTMLElement>(".edit-profile-page");
    if (!root) throw new Error("no form");
    new ProfileEditForm(root).install();
}

function change(selector: string, value: string): void {
    const el = document.querySelector<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(selector);
    if (!el) throw new Error(`no ${selector}`);
    el.value = value;
    el.dispatchEvent(new Event("change", { bubbles: true }));
}

beforeAll(() => {
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: String(input), body: init?.body instanceof FormData ? Object.fromEntries(init.body) : {} });
        return respond();
    }, realFetch);
    // happy-dom's dialog does not open modally; recording the call is enough here.
    HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
        this.setAttribute("open", "");
    };
});

afterAll(() => {
    globalThis.fetch = realFetch;
    window.toastr = realToastr;
    HTMLDialogElement.prototype.showModal = realShowModal;
});

beforeEach(() => {
    calls = [];
    toasts = [];
    respond = () => new Response(JSON.stringify({ ok: true }), { status: 200 });
    window.toastr = { success: () => undefined, error: (m) => void toasts.push(m), warning: () => undefined, info: () => undefined, clear: () => undefined };
});

describe("date errors", () => {
    test("the server's message is shown as text, never as markup", async () => {
        respond = () => new Response(JSON.stringify({ error: '<img src=x onerror="window.pwned=1">' }), { status: 200 });
        render();
        change('[data-autosave="birth_date"]', "2030-01-01");
        await settle();
        const dialog = document.getElementById("date-error-dialog");
        expect(dialog?.hasAttribute("open")).toBe(true);
        expect(dialog?.querySelector("img")).toBeNull();
        expect(dialog?.querySelector(".date-error-list")?.textContent).toBe('<img src=x onerror="window.pwned=1">');
    });

    test("a refused date clears the field and says why", async () => {
        respond = () => new Response(JSON.stringify({ error: "You must be at least 18 years old to use this service." }), { status: 200 });
        render();
        change('[data-autosave="birth_date"]', "2020-01-01");
        await settle();
        expect(document.querySelector<HTMLInputElement>('[data-autosave="birth_date"]')?.value).toBe("");
        expect(document.querySelector('[data-field-status="birth_date"]')?.textContent).toBe("✗");
        expect(toasts).toEqual(["You must be at least 18 years old to use this service."]);
    });

    test("a date refused when the form was posted opens on load", () => {
        render(true);
        expect(document.getElementById("date-error-dialog")?.hasAttribute("open")).toBe(true);
    });
});

describe("autosave", () => {
    test("a change saves that field", async () => {
        render();
        change('[data-autosave="bio"]', "Rooftops and tunnels.");
        await settle();
        expect(calls).toEqual([{ url: "/profile/field/", body: { field: "bio", value: "Rooftops and tunnels." } }]);
        expect(document.querySelector('[data-field-status="bio"]')?.textContent).toBe("✓ Saved");
    });

    test("a save that doesn't get through keeps what was typed", async () => {
        respond = () => new Response("<h1>Server Error</h1>", { status: 500 });
        render();
        change('[data-autosave="bio"]', "Rooftops and tunnels.");
        await settle();
        expect(document.querySelector<HTMLTextAreaElement>('[data-autosave="bio"]')?.value).toBe("Rooftops and tunnels.");
        expect(document.querySelector('[data-field-status="bio"]')?.textContent).toBe("Save failed.");
        expect(toasts).toEqual([]);
    });

    test("choosing Other reveals the field to say what", () => {
        render();
        change('[data-preference-toggle="contact_pref"]', "other");
        expect(document.querySelector<HTMLElement>('[data-preference-other-for="contact_pref"]')?.style.display).toBe("");
        change('[data-preference-toggle="contact_pref"]', "dm");
        expect(document.querySelector<HTMLElement>('[data-preference-other-for="contact_pref"]')?.style.display).toBe("none");
    });
});

describe("avatar and username", () => {
    test("a new avatar shows in the form and in the page hero", async () => {
        respond = () => new Response(JSON.stringify({ ok: true, avatar_url: "/media/a.png" }), { status: 200 });
        render();
        document.getElementById("avatar-gravatar-btn")?.click();
        await settle();
        expect(calls[0]?.body).toEqual({ field: "avatar_gravatar" });
        const preview = document.getElementById("avatar-preview");
        const hero = document.getElementById("profile-hero-avatar");
        expect(preview instanceof HTMLImageElement && preview.className === "edit-avatar-preview").toBe(true);
        expect(hero instanceof HTMLImageElement && hero.className === "profile-avatar-img").toBe(true);
    });

    test("a malformed username shows the rule and is not saved on blur", async () => {
        render();
        const input = document.getElementById("username-input");
        if (!(input instanceof HTMLInputElement)) throw new Error("no username input");
        input.value = "a b";
        input.dispatchEvent(new Event("input"));
        input.dispatchEvent(new Event("blur"));
        await settle(450);
        expect(calls).toEqual([]);
        expect(document.getElementById("username-requirements-hint")?.style.display).toBe("");
        expect(document.getElementById("username-hint")?.className).toBe("edit-username-hint edit-username-hint--bad");
    });

    test("a refused username keeps what was typed and says why", async () => {
        respond = () => new Response(JSON.stringify({ error: "That username is unavailable." }), { status: 400 });
        render();
        const input = document.getElementById("username-input");
        if (!(input instanceof HTMLInputElement)) throw new Error("no username input");
        input.value = "owl_two";
        input.dispatchEvent(new Event("blur"));
        await settle();
        expect(calls[0]?.body).toEqual({ field: "username", value: "owl_two" });
        expect(input.value).toBe("owl_two");
        expect(toasts).toEqual(["That username is unavailable."]);
    });
});
