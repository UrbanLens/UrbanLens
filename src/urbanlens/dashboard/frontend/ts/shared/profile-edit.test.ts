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
      <div id="profile-hero-avatar" class="profile-avatar-placeholder" data-user-avatar="profile-avatar-img">JM</div>
      <header>
        <span class="nav-avatar-initial" data-user-avatar="nav-avatar-img" data-avatar-alt="">J</span>
        <img class="nav-avatar-img" data-user-avatar="nav-avatar-img" data-avatar-alt="" src="/media/avatars/old.png" alt="">
      </header>
      <img id="someone-else" class="profile-avatar-img" src="/media/avatars/friend.png" alt="Avatar">
      <dialog id="date-error-dialog" ${openOnLoad ? "data-open-on-load" : ""}>
        <ul class="date-error-list"><li>Server-rendered error</li></ul>
      </dialog>
      <div class="edit-profile-page" data-save-url="/profile/field/" data-map-url="/map/">
        <button type="button" id="setup-skip-btn">Skip</button>
        <span data-field-status="username"></span>
        <input id="username-input" value="owl_one">
        <span id="username-hint"></span>
        <p id="username-requirements-hint" style="display:none"></p>
        <div class="edit-avatar-placeholder" id="avatar-preview" data-user-avatar="edit-avatar-preview">JM</div>
        <span data-field-status="avatar"></span>
        <input type="file" data-autosave="avatar">
        <button type="button" id="avatar-gravatar-btn" data-gravatar-url="/static/dashboard/gravatar-copy.png">Gravatar</button>
        <button type="button" class="edit-avatar-emoji-opt" data-animal="owl" data-color="teal">Owl</button>
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
    test("a suggested icon shows in the form, the page hero and the navbar", async () => {
        respond = () => new Response(JSON.stringify({ ok: true, avatar_url: "/media/a.png" }), { status: 200 });
        render();
        document.querySelector<HTMLElement>(".edit-avatar-emoji-opt")?.click();
        await settle();
        expect(calls[0]?.body).toEqual({ field: "avatar_emoji", animal: "owl", color: "teal" });
        const preview = document.getElementById("avatar-preview");
        const hero = document.getElementById("profile-hero-avatar");
        expect(preview instanceof HTMLImageElement && preview.className === "edit-avatar-preview").toBe(true);
        expect(hero instanceof HTMLImageElement && hero.className === "profile-avatar-img").toBe(true);
        const nav = [...document.querySelectorAll("header [data-user-avatar]")];
        expect(nav.map((el) => el instanceof HTMLImageElement && el.className === "nav-avatar-img" && el.alt === "" && el.getAttribute("src")?.startsWith("/media/a.png?"))).toEqual([true, true]);
        // An avatar of someone else's on the page is not the user's to change.
        expect(document.getElementById("someone-else")?.getAttribute("src")).toBe("/media/avatars/friend.png");
        expect(document.querySelector('[data-field-status="avatar"]')?.textContent).toBe("✓ Saved");
    });

    test("a site that serves uploads from their own origin has its avatar drawn from there, and from no other", async () => {
        const MEDIA = "https://media.example.org";
        respond = () => new Response(JSON.stringify({ ok: true, avatar_url: `${MEDIA}/media/avatars/owl.png` }), { status: 200 });
        render();
        document.querySelector(".edit-profile-page")?.setAttribute("data-media-origin", MEDIA);
        document.querySelector<HTMLElement>(".edit-avatar-emoji-opt")?.click();
        await settle();
        expect(document.getElementById("profile-hero-avatar")?.getAttribute("src")).toStartWith(`${MEDIA}/media/avatars/owl.png?v=`);
        expect(document.querySelector(".nav-avatar-img")?.getAttribute("src")).toStartWith(`${MEDIA}/media/avatars/owl.png?v=`);

        // Another origin is not the configured one, whatever the attribute says.
        respond = () => new Response(JSON.stringify({ ok: true, avatar_url: "https://evil.example/media/avatars/owl.png" }), { status: 200 });
        const warn = console.warn;
        console.warn = () => undefined;
        document.querySelector<HTMLElement>(".edit-avatar-emoji-opt")?.click();
        await settle();
        console.warn = warn;
        expect(document.getElementById("profile-hero-avatar")?.getAttribute("src")).toStartWith(`${MEDIA}/media/avatars/owl.png?v=`);
    });

    test("without a media origin on the page, an avatar on another origin is not drawn", async () => {
        respond = () => new Response(JSON.stringify({ ok: true, avatar_url: "https://media.example.org/media/avatars/owl.png" }), { status: 200 });
        render();
        const warn = console.warn;
        console.warn = () => undefined;
        document.querySelector<HTMLElement>(".edit-avatar-emoji-opt")?.click();
        await settle();
        console.warn = warn;
        expect(document.getElementById("profile-hero-avatar") instanceof HTMLImageElement).toBe(false);
        expect(document.querySelector(".nav-avatar-img")?.getAttribute("src")).toBe("/media/avatars/old.png");
    });

    test("an upload shows at once from the user's own file, though the server answers with the picture it replaces", async () => {
        respond = () => new Response(JSON.stringify({ ok: true, avatar_url: "/media/avatars/old.png", avatar_pending: true }), { status: 200 });
        render();
        const file = new File([new Uint8Array([137, 80, 78, 71])], "me.png", { type: "image/png" });
        const input = document.querySelector<HTMLInputElement>('input[type="file"]');
        if (!input) throw new Error("no file input");
        Object.defineProperty(input, "files", { value: [file], configurable: true });
        input.dispatchEvent(new Event("change", { bubbles: true }));
        await settle();
        expect(calls[0]?.body.field).toBe("avatar");
        const shown = ["avatar-preview", "profile-hero-avatar"].map((id) => document.getElementById(id));
        expect(shown.every((el) => el instanceof HTMLImageElement && el.getAttribute("src")?.startsWith("blob:"))).toBe(true);
        expect([...document.querySelectorAll("header [data-user-avatar]")].every((el) => el.getAttribute("src")?.startsWith("blob:"))).toBe(true);
        expect(document.querySelector('[data-field-status="avatar"]')?.textContent).toBe("✓ Processing…");
    });

    test("an upload the browser cannot draw waits for the stored copy instead of showing a broken image", async () => {
        respond = () => new Response(JSON.stringify({ ok: true, avatar_url: null, avatar_pending: true }), { status: 200 });
        render();
        const file = new File([new Uint8Array([0])], "me.heic", { type: "image/heic" });
        const input = document.querySelector<HTMLInputElement>('input[type="file"]');
        if (!input) throw new Error("no file input");
        Object.defineProperty(input, "files", { value: [file], configurable: true });
        input.dispatchEvent(new Event("change", { bubbles: true }));
        await settle();
        expect(document.getElementById("avatar-preview") instanceof HTMLImageElement).toBe(false);
    });

    test("a Gravatar shows from the site's copy of it", async () => {
        respond = () => new Response(JSON.stringify({ ok: true, avatar_url: null, avatar_pending: true }), { status: 200 });
        render();
        document.getElementById("avatar-gravatar-btn")?.click();
        await settle();
        expect(calls[0]?.body).toEqual({ field: "avatar_gravatar" });
        const preview = document.getElementById("avatar-preview");
        expect(preview instanceof HTMLImageElement && preview.getAttribute("src")?.startsWith("/static/dashboard/gravatar-copy.png?")).toBe(true);
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
