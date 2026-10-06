import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { saveAvatar, saveProfileField, showAvatar, showSavedAvatar, showUserAvatar, usernameAvailability, withVersion } from "./profile-field";

const realFetch = globalThis.fetch;
const realToken = window.csrftoken;
let respond: () => Response = () => new Response(JSON.stringify({ ok: true }), { status: 200 });
let sent: { url: string; headers: HeadersInit | undefined; body: Record<string, FormDataEntryValue> } | null = null;

beforeAll(() => {
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL, init?: RequestInit) => {
        sent = { url: String(input), headers: init?.headers, body: init?.body instanceof FormData ? Object.fromEntries(init.body) : {} };
        return respond();
    }, realFetch);
});

afterAll(() => {
    globalThis.fetch = realFetch;
    window.csrftoken = realToken;
});

beforeEach(() => {
    sent = null;
    window.csrftoken = "tok123";
    respond = () => new Response(JSON.stringify({ ok: true }), { status: 200 });
});

describe("saveProfileField", () => {
    test("posts the field with the CSRF header", async () => {
        expect(await saveProfileField("/f/", "username", "owl")).toEqual({ ok: true });
        expect(sent?.body).toEqual({ field: "username", value: "owl" });
        expect(sent?.headers).toEqual({ "X-CSRFToken": "tok123" });
    });

    test("a refusal in a 200 and a refusal in a 400 read the same", async () => {
        respond = () => new Response(JSON.stringify({ ok: false, error: "Not a date." }), { status: 200 });
        expect(await saveProfileField("/f/", "birthday", "x")).toEqual({ ok: false, refused: true, error: "Not a date." });
        respond = () => new Response(JSON.stringify({ error: "Not a date." }), { status: 400 });
        expect(await saveProfileField("/f/", "birthday", "x")).toEqual({ ok: false, refused: true, error: "Not a date." });
    });

    test("a server error is a failed save, not a refusal of the value", async () => {
        respond = () => new Response("<h1>Server Error</h1>", { status: 500 });
        expect(await saveProfileField("/f/", "bio", "x")).toEqual({ ok: false, refused: false, error: undefined });
    });

    test("a server that can't save right now still says why", async () => {
        respond = () => new Response(JSON.stringify({ error: "Our antivirus scanner is temporarily unavailable." }), { status: 503 });
        expect(await saveProfileField("/f/", "avatar", "x")).toEqual({ ok: false, refused: false, error: "Our antivirus scanner is temporarily unavailable." });
    });

    test("an emoji avatar sends its animal and colour", async () => {
        await saveAvatar("/f/", { kind: "emoji", animal: "fox", color: "red" });
        expect(sent?.body).toEqual({ field: "avatar_emoji", animal: "fox", color: "red" });
    });
});

describe("usernameAvailability", () => {
    test("says why a name is taken, and null when the check failed", async () => {
        respond = () => new Response(JSON.stringify({ available: false, reason: "Taken." }), { status: 200 });
        expect(await usernameAvailability("/f/", "a b")).toEqual({ available: false, reason: "Taken." });
        expect(sent?.url).toBe("/f/?field=username&value=a%20b");
        respond = () => new Response("", { status: 500 });
        expect(await usernameAvailability("/f/", "owl")).toBeNull();
    });
});

describe("withVersion", () => {
    test("adds a v parameter to a path, keeping what it already has", () => {
        expect(withVersion("/media/a.png", 7)).toBe("/media/a.png?v=7");
        expect(withVersion("/media/a.png?sig=abc&v=1#x", 7)).toBe("/media/a.png?sig=abc&v=7#x");
    });

    test("leaves another origin's address on that origin and a blob or data URL alone", () => {
        expect(withVersion("https://cdn.example.com/a.png?x=1", 7)).toBe("https://cdn.example.com/a.png?x=1&v=7");
        expect(withVersion("blob:http://localhost/1234", 7)).toBe("blob:http://localhost/1234");
        expect(withVersion("data:image/png;base64,AAAA", 7)).toBe("data:image/png;base64,AAAA");
    });
});

describe("showAvatar", () => {
    test("replaces a placeholder with an image, and reloads an image in place", () => {
        document.body.innerHTML = `<div id="av">JM</div>`;
        showAvatar(document.getElementById("av"), "/a.png", "avatar");
        const img = document.getElementById("av");
        expect(img instanceof HTMLImageElement && img.className === "avatar").toBe(true);
        showAvatar(img, "/b.png", "avatar");
        expect(document.getElementById("av")).toBe(img);
        expect(img?.getAttribute("src")).toStartWith("/b.png?v=");
    });

    test("the image replacing a placeholder keeps the marks that make it the user's avatar", () => {
        document.body.innerHTML = `<span id="nav" class="initial" data-user-avatar="nav-img" data-avatar-alt="">J</span>`;
        showUserAvatar("/a.png");
        const img = document.getElementById("nav");
        expect(img instanceof HTMLImageElement && img.className === "nav-img" && img.alt === "" && img.dataset.userAvatar === "nav-img").toBe(true);
        // ... so the next one finds it again.
        showUserAvatar("/b.png");
        expect(document.getElementById("nav")?.getAttribute("src")).toStartWith("/b.png?v=");
    });
});

describe("showSavedAvatar", () => {
    const now = async () => undefined;
    const placeholders = () => {
        document.body.innerHTML = `
          <div id="hero" class="hero-placeholder" data-user-avatar="hero-img">J</div>
          <img id="nav" class="nav-img" data-user-avatar="nav-img" src="/media/old.png" alt="">`;
    };
    const srcs = () => ["hero", "nav"].map((id) => document.getElementById(id)?.getAttribute("src") ?? document.getElementById(id)?.tagName);
    let states: (Response | Error)[] = [];
    let asked = 0;

    beforeEach(() => {
        states = [];
        asked = 0;
        globalThis.fetch = Object.assign(async () => {
            asked++;
            const next = states.shift();
            if (next instanceof Error) throw next;
            return next ?? new Response(JSON.stringify({ avatar_url: "/media/old.png", avatar_pending: true }), { status: 200 });
        }, realFetch);
    });

    const state = (avatar_url: string | null, avatar_pending: boolean) => new Response(JSON.stringify({ avatar_url, avatar_pending }), { status: 200 });

    test("a stored picture is drawn from the address the save answered with", async () => {
        placeholders();
        expect(await showSavedAvatar("/f/", { kind: "emoji", animal: "owl", color: "teal" }, { ok: true, avatar_url: "/media/new.svg" }, { sleep: now })).toBe("shown");
        expect(srcs().every((src) => src?.startsWith("/media/new.svg?v="))).toBe(true);
        expect(asked).toBe(0);
    });

    test("a held Gravatar is drawn from its preview, then replaced by the stored copy once it exists", async () => {
        placeholders();
        states = [state("/media/old.png", true), state("/media/new.png", false)];
        const outcome = showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/g.png" }, { ok: true, avatar_url: "/media/old.png", avatar_pending: true }, { sleep: now });
        expect(srcs().every((src) => src?.startsWith("/static/g.png?v="))).toBe(true);
        expect(await outcome).toBe("published");
        expect(srcs().every((src) => src?.startsWith("/media/new.png?v="))).toBe(true);
        expect(asked).toBe(2);
    });

    test("an upload that was dropped puts the stored picture back", async () => {
        document.body.innerHTML = `
          <img id="hero" class="hero-img" data-user-avatar="hero-img" src="/media/old.png" alt="">
          <img id="nav" class="nav-img" data-user-avatar="nav-img" src="/media/old.png" alt="">`;
        states = [state("/media/old.png", false)];
        const outcome = await showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/g.png" }, { ok: true, avatar_url: "/media/old.png", avatar_pending: true }, { sleep: now });
        expect(outcome).toBe("unchanged");
        expect(srcs().every((src) => src?.startsWith("/media/old.png?v="))).toBe(true);
    });

    test("an upload that was dropped, with no picture stored, puts the placeholder back", async () => {
        document.body.innerHTML = `<div id="hero" class="hero-placeholder" data-user-avatar="hero-img">J</div><div id="nav" class="nav-placeholder" data-user-avatar="nav-img">J</div>`;
        states = [state(null, false)];
        const outcome = await showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/g.png" }, { ok: true, avatar_url: null, avatar_pending: true }, { sleep: now });
        expect(outcome).toBe("unchanged");
        expect(srcs()).toEqual(["DIV", "DIV"]);
        expect(document.getElementById("hero")?.textContent).toBe("J");
    });

    test("a held choice dropped after an earlier held one shows what is stored, not the earlier one's preview", async () => {
        placeholders();
        let release: () => void = () => undefined;
        const held = new Promise<void>((resolve) => (release = resolve));
        const first = showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/A.png" }, { ok: true, avatar_url: "/media/old.png", avatar_pending: true }, { sleep: () => held });
        expect(srcs().every((src) => src?.startsWith("/static/A.png?v="))).toBe(true);
        states = [state("/media/old.png", false)];
        const second = await showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/B.png" }, { ok: true, avatar_url: "/media/old.png", avatar_pending: true }, { sleep: now });
        expect(second).toBe("unchanged");
        expect(srcs().every((src) => src?.startsWith("/media/old.png?v="))).toBe(true);
        release();
        expect(await first).toBe("superseded");
    });

    test("with nothing stored, the placeholder an earlier held choice replaced comes back", async () => {
        document.body.innerHTML = `<div id="hero" class="hero-placeholder" data-user-avatar="hero-img">J</div>`;
        let release: () => void = () => undefined;
        const held = new Promise<void>((resolve) => (release = resolve));
        const first = showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/A.png" }, { ok: true, avatar_url: null, avatar_pending: true }, { sleep: () => held });
        states = [state(null, false)];
        await showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/B.png" }, { ok: true, avatar_url: null, avatar_pending: true }, { sleep: now });
        expect(document.getElementById("hero")?.tagName).toBe("DIV");
        release();
        await first;
    });

    test("the previous picture's address coming back with another query string is still the same picture", async () => {
        placeholders();
        states = [state("/media/old.png?sig=later", false)];
        const outcome = await showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/g.png" }, { ok: true, avatar_url: "/media/old.png?sig=first", avatar_pending: true }, { sleep: now });
        expect(outcome).toBe("unchanged");
    });

    test("an upload the browser cannot draw is left to the stored copy", async () => {
        placeholders();
        states = [state("/media/new.png", false)];
        const heic = new File([new Uint8Array([0])], "me.heic", { type: "image/heic" });
        const outcome = showSavedAvatar("/f/", { kind: "upload", file: heic }, { ok: true, avatar_url: null, avatar_pending: true }, { sleep: now });
        expect(srcs()).toEqual(["DIV", "/media/old.png"]);
        expect(await outcome).toBe("published");
        expect(srcs().every((src) => src?.startsWith("/media/new.png?v="))).toBe(true);
    });

    test("a wait that outlasts its tries leaves the chosen picture on show", async () => {
        placeholders();
        states = [state("/media/old.png", true), new Error("offline")];
        const outcome = await showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/g.png" }, { ok: true, avatar_url: "/media/old.png", avatar_pending: true }, { sleep: now, delaysMs: [1, 1, 1] });
        expect(outcome).toBe("waiting");
        expect(srcs().every((src) => src?.startsWith("/static/g.png?v="))).toBe(true);
    });

    test("a later choice ends the wait for an earlier one", async () => {
        placeholders();
        let release: () => void = () => undefined;
        const held = new Promise<void>((resolve) => (release = resolve));
        const first = showSavedAvatar("/f/", { kind: "gravatar", previewUrl: "/static/g.png" }, { ok: true, avatar_url: "/media/old.png", avatar_pending: true }, { sleep: () => held });
        expect(await showSavedAvatar("/f/", { kind: "emoji", animal: "owl", color: "teal" }, { ok: true, avatar_url: "/media/owl.svg" }, { sleep: now })).toBe("shown");
        release();
        expect(await first).toBe("superseded");
        expect(srcs().every((src) => src?.startsWith("/media/owl.svg?v="))).toBe(true);
        expect(asked).toBe(0);
    });
});
