import { afterEach, beforeEach, expect, test } from "bun:test";

import { FormAutosave, type FormAutosaveOptions } from "./form-autosave";

const realFetch = globalThis.fetch;
const realToastr = window.toastr;
const realGuard = window.autosaveGuard;
let respond: () => Response | Promise<Response>;
let posts: { body: string; reported: unknown }[];
let toasts: string[];
let guard: string[];
let timers: { fn: () => void; ms: number }[];

const OPTIONS: FormAutosaveOptions = {
    actionsSelector: ".form-actions",
    submitSelector: ".btn--submit",
    changeDelay: (control) => (control.type === "hidden" ? (control.id === "priority" ? 0 : null) : control.type === "number" ? 1200 : 0),
    inputDelay: (control) => (control.type === "number" ? 1200 : null),
};

function render(): HTMLFormElement {
    document.body.innerHTML = `
      <form action="/admin/" id="f">
        <input type="number" name="max_trip_members" value="5">
        <input type="checkbox" name="signup_restricted">
        <input type="hidden" id="priority" name="priority" value="a,b">
        <input type="hidden" name="other" value="x">
        <div class="form-actions"><button type="submit" class="btn--submit">Save</button><span class="form-autosave-indicator"></span></div>
      </form>`;
    const form = document.querySelector("form");
    if (!(form instanceof HTMLFormElement)) throw new Error("no form");
    return form;
}

const later = (fn: () => void, ms: number): void => void timers.push({ fn, ms });
const runTimers = async () => {
    const due = timers;
    timers = [];
    for (const { fn } of due) fn();
    for (let i = 0; i < 4; i++) await new Promise((resolve) => setTimeout(resolve, 0));
};
const input = (name: string) => document.querySelector<HTMLInputElement>(`[name='${name}']`);
const fire = (el: Element | null, type: string) => el?.dispatchEvent(new Event(type, { bubbles: true }));
const indicator = () => document.querySelector<HTMLElement>(".form-autosave-indicator");

beforeEach(() => {
    posts = [];
    toasts = [];
    guard = [];
    timers = [];
    respond = () => new Response(JSON.stringify({ ok: true, values: {} }), { status: 200, headers: { "Content-Type": "application/json" } });
    globalThis.fetch = Object.assign(
        async (_input: RequestInfo | URL, init?: RequestInit) => {
            const body = init?.body;
            posts.push({ body: body instanceof FormData ? new URLSearchParams([...body.entries()].map(([k, v]) => [k, String(v)])).toString() : "", reported: init ? Reflect.get(init, "__ulReported") : undefined });
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
    window.autosaveGuard = {
        markDirty: () => guard.push("dirty"),
        markClean: () => guard.push("clean"),
        saveStarted: () => guard.push("started"),
        saveFinished: () => guard.push("finished"),
        allowNavigation: () => undefined,
        isBlocked: () => false,
        setMessage: () => undefined,
    };
});

afterEach(() => {
    globalThis.fetch = realFetch;
    window.toastr = realToastr;
    window.autosaveGuard = realGuard;
});

test("attaching hides the save button and reuses the rendered indicator", () => {
    const form = render();
    new FormAutosave(OPTIONS, later).attach(form);
    expect(document.querySelector<HTMLElement>(".btn--submit")?.style.display).toBe("none");
    expect(document.querySelectorAll(".form-autosave-indicator")).toHaveLength(1);
});

test("typing in a form with no id is debounced into one save", async () => {
    const form = render();
    form.removeAttribute("id");
    new FormAutosave(OPTIONS, later).attach(form);
    const field = input("max_trip_members");
    for (const value of ["1", "12", "123"]) {
        if (field) field.value = value;
        fire(field, "input");
    }
    fire(field, "change");
    await runTimers();
    expect(posts).toHaveLength(1);
    expect(posts[0]?.body).toContain("max_trip_members=123");
    expect(posts[0]?.reported).toBe(true);
});

test("a clamped value is repainted, unless the field has focus", async () => {
    respond = () => new Response(JSON.stringify({ ok: true, values: { max_trip_members: 50 } }), { headers: { "Content-Type": "application/json" } });
    const form = render();
    new FormAutosave(OPTIONS, later).attach(form);
    const field = input("max_trip_members");
    if (field) field.value = "9999";
    fire(field, "change");
    await runTimers();
    expect(field?.value).toBe("50");
    expect(indicator()?.textContent).toBe("✓ Saved");
    expect(indicator()?.classList.contains("is-shown")).toBe(true);
    expect(guard).toEqual(["dirty", "started", "clean", "finished"]);

    field?.focus();
    if (field) field.value = "9999";
    fire(field, "change");
    await runTimers();
    expect(field?.value).toBe("9999");
});

test("only the hidden fields a page names trigger a save", async () => {
    const form = render();
    new FormAutosave(OPTIONS, later).attach(form);
    fire(input("other"), "change");
    await runTimers();
    expect(posts).toHaveLength(0);
    fire(input("priority"), "change");
    await runTimers();
    expect(posts).toHaveLength(1);
});

test("a refusal names the first error, once", async () => {
    respond = () => new Response(JSON.stringify({ ok: false, errors: { max_trip_members: ["Too many."] } }), { headers: { "Content-Type": "application/json" } });
    const form = render();
    new FormAutosave(OPTIONS, later).attach(form);
    fire(input("signup_restricted"), "change");
    await runTimers();
    expect(toasts).toEqual(["error:Too many."]);
    expect(indicator()?.textContent).toBe("Too many.");
    expect(indicator()?.classList.contains("form-autosave-indicator--error")).toBe(true);
    expect(guard).toContain("dirty");
    expect(guard).not.toContain("clean");
});

test("a failed response or a lost connection is reported, not swallowed", async () => {
    respond = () => new Response("<h1>Server Error</h1>", { status: 500, headers: { "Content-Type": "text/html" } });
    const form = render();
    const autosave = new FormAutosave(OPTIONS, later);
    autosave.attach(form);
    fire(input("signup_restricted"), "change");
    await runTimers();
    expect(toasts).toEqual(["error:Could not save your changes. Please try again."]);
    expect(guard).not.toContain("clean");

    toasts = [];
    respond = () => Promise.reject(new TypeError("Failed to fetch"));
    autosave.schedule(form, 0);
    await runTimers();
    expect(toasts).toEqual(["error:Your changes were not saved. Check your connection and try again."]);
    expect(indicator()?.textContent).toBe("Not saved");
});

test("a save succeeds on a plain 2xx without JSON", async () => {
    respond = () => new Response("<html></html>", { status: 200, headers: { "Content-Type": "text/html" } });
    const form = render();
    new FormAutosave(OPTIONS, later).attach(form);
    fire(input("signup_restricted"), "change");
    await runTimers();
    expect(indicator()?.textContent).toBe("✓ Saved");
    expect(toasts).toEqual([]);
});

test("a form holding a password is never autosaved, so the raw secret never leaves the page", async () => {
    document.body.innerHTML = `
      <form id="password-change-form" class="settings-form">
        <input type="password" name="current_password">
        <input type="text" name="note">
        <div class="form-actions"><button type="submit" class="btn--submit">Change password</button></div>
      </form>`;
    const form = document.querySelector("form");
    if (!(form instanceof HTMLFormElement)) throw new Error("no form");
    new FormAutosave(OPTIONS, later).attach(form);
    const secret = form.querySelector<HTMLInputElement>("[name=current_password]");
    if (secret) secret.value = "Sekrit-Canary-9";
    fire(secret, "change");
    fire(form.querySelector("[name=note]"), "change");
    await runTimers();
    expect(posts).toEqual([]);
    expect(form.querySelector<HTMLElement>(".btn--submit")?.style.display).toBe("");
});
