import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { saveAvatar, saveProfileField, showAvatar, usernameAvailability } from "./profile-field";

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
        expect(await saveProfileField("/f/", "birthday", "x")).toEqual({ ok: false, error: "Not a date." });
        respond = () => new Response(JSON.stringify({ error: "Not a date." }), { status: 400 });
        expect(await saveProfileField("/f/", "birthday", "x")).toEqual({ ok: false, error: "Not a date." });
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

describe("showAvatar", () => {
    test("replaces a placeholder with an image, and reloads an image in place", () => {
        document.body.innerHTML = `<div id="av">JM</div>`;
        showAvatar(document.getElementById("av"), "/a.png", "avatar");
        const img = document.getElementById("av");
        expect(img instanceof HTMLImageElement && img.className === "avatar").toBe(true);
        showAvatar(img, "/b.png", "avatar");
        expect(document.getElementById("av")).toBe(img);
        expect(img?.getAttribute("src")).toStartWith("/b.png?");
    });
});
