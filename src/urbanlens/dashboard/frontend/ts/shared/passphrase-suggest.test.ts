import { afterEach, beforeEach, expect, test } from "bun:test";

import { installPassphraseSuggest } from "./passphrase-suggest";

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const realFetch = globalThis.fetch;
let respond: () => Response = () => new Response(JSON.stringify({ passphrases: ["amber-otter-quarry-7", "<b>bold</b>-vault-2"] }), { status: 200 });
let requests: { url: string; reported: unknown }[] = [];
const heard: string[] = [];

beforeEach(() => {
    requests = [];
    heard.length = 0;
    respond = () => new Response(JSON.stringify({ passphrases: ["amber-otter-quarry-7", "<b>bold</b>-vault-2"] }), { status: 200 });
    globalThis.fetch = Object.assign(
        async (input: RequestInfo | URL, init?: RequestInit) => {
            requests.push({ url: String(input), reported: init ? Reflect.get(init, "__ulReported") : undefined });
            return respond();
        },
        { preconnect: realFetch.preconnect },
    );
    document.body.innerHTML = `
      <input type="password" id="pw"><input type="password" id="pw2">
      <div class="auth-passphrase" data-suggest-url="/suggest/" data-password-id="pw" data-confirm-id="pw2">
        <button type="button" data-passphrase-suggest>Suggest</button>
        <ul data-passphrase-list hidden></ul>
        <p data-passphrase-status hidden></p>
      </div>`;
    document.getElementById("pw")?.addEventListener("input", () => heard.push("pw"));
    installPassphraseSuggest(document);
});

afterEach(() => {
    globalThis.fetch = realFetch;
});

const status = () => document.querySelector<HTMLElement>("[data-passphrase-status]");
const suggest = () => document.querySelector<HTMLElement>("[data-passphrase-suggest]")?.click();

test("lists the suggestions as text, and picking one fills both fields visibly", async () => {
    suggest();
    await settle();
    await settle();
    const options = Array.from(document.querySelectorAll<HTMLButtonElement>("[data-passphrase-list] button"));
    expect(options.map((o) => o.textContent)).toEqual(["amber-otter-quarry-7", "<b>bold</b>-vault-2"]);
    expect(document.querySelector("[data-passphrase-list] b")).toBeNull();
    expect(status()?.textContent).toBe("Click a passphrase to use it.");
    expect(requests).toEqual([{ url: "/suggest/", reported: true }]);

    options[0]?.click();
    for (const id of ["pw", "pw2"]) {
        const input = document.getElementById(id);
        expect(input instanceof HTMLInputElement ? [input.value, input.type] : null).toEqual(["amber-otter-quarry-7", "text"]);
    }
    expect(heard).toEqual(["pw"]);
    expect(status()?.textContent).toContain("Copy it somewhere safe");
});

test("a throttle says to wait", async () => {
    respond = () => new Response("", { status: 429 });
    suggest();
    await settle();
    await settle();
    expect(status()?.textContent).toBe("Too many requests. Try again in a few minutes.");
    expect(status()?.classList.contains("auth-passphrase-status--error")).toBe(true);
    expect(document.querySelector<HTMLButtonElement>("[data-passphrase-suggest]")?.disabled).toBe(false);
});

test("any other failure is generic", async () => {
    respond = () => new Response("", { status: 500 });
    suggest();
    await settle();
    await settle();
    expect(status()?.textContent).toBe("Could not load suggestions.");
});
