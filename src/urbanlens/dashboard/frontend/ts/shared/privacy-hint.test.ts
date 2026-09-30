import { afterEach, beforeAll, beforeEach, expect, test } from "bun:test";

import { installPrivacyHints } from "./privacy-hint";

const realFetch = globalThis.fetch;
const realToastr = window.toastr;
let toasts: string[];
let posts: { url: string; body: string; reported: unknown }[];
let respond: () => Response | Promise<Response>;

const HINT = (field: string, label: string, value: string, display: string) => `
  <span class="ul-privacy-hint ul-privacy-hint--editable" data-hint-label="${label}">
    <button type="button" class="ul-privacy-hint-btn" title="${label} - Visible to: ${display} - click to change"
            aria-label="Change who can see this - currently visible to: ${display}"><i>lock</i></button>
    <select class="ul-privacy-hint-select" hidden data-field="${field}" data-url="/settings/privacy/${field}/">
      <option value="anyone"${value === "anyone" ? " selected" : ""}>Anyone (Logged In)</option>
      <option value="friends"${value === "friends" ? " selected" : ""}>Friends Only</option>
      <option value="no_one"${value === "no_one" ? " selected" : ""}>No one</option>
    </select>
  </span>`;

const settle = async () => {
    for (let i = 0; i < 4; i++) await new Promise((resolve) => setTimeout(resolve, 0));
};
const selects = (field: string) => Array.from(document.querySelectorAll<HTMLSelectElement>(`.ul-privacy-hint-select[data-field="${field}"]`));
const choose = (select: HTMLSelectElement | undefined, value: string) => {
    if (!select) throw new Error("no select");
    const button = select.previousElementSibling;
    if (button instanceof HTMLElement) button.click();
    select.value = value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
};

beforeAll(() => installPrivacyHints());

beforeEach(() => {
    toasts = [];
    posts = [];
    respond = () => new Response(JSON.stringify({ ok: true, value: "friends", display: "Friends Only" }), { status: 200, headers: { "Content-Type": "application/json" } });
    globalThis.fetch = Object.assign(
        async (input: RequestInfo | URL, init?: RequestInit) => {
            const body = init?.body;
            posts.push({ url: String(input), body: body instanceof FormData ? new URLSearchParams([...body.entries()].map(([k, v]) => [k, String(v)])).toString() : "", reported: init ? Reflect.get(init, "__ulReported") : undefined });
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
    document.body.innerHTML = HINT("profile_visibility", "Name, avatar &amp; bio", "anyone", "Anyone (Logged In)") + HINT("profile_visibility", "Social", "anyone", "Anyone (Logged In)") + HINT("contact_visibility", "Contact", "no_one", "No one");
    // happy-dom misreads `selected` on all but the first select parsed from one innerHTML.
    for (const select of document.querySelectorAll<HTMLSelectElement>("select")) {
        select.value = Array.from(select.options).find((option) => option.hasAttribute("selected"))?.value ?? "";
    }
});

afterEach(() => {
    globalThis.fetch = realFetch;
    window.toastr = realToastr;
});

test("the lock opens the picker in its place", () => {
    const [select] = selects("profile_visibility");
    const button = select?.previousElementSibling;
    if (!(button instanceof HTMLElement)) throw new Error("no button");
    button.click();
    expect(button.hidden).toBe(true);
    expect(select?.hidden).toBe(false);
    select?.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
    expect(button.hidden).toBe(false);
    expect(select?.hidden).toBe(true);
});

test("a choice posts only that setting, and every hint for it follows", async () => {
    choose(selects("profile_visibility")[0], "friends");
    await settle();
    expect(posts).toEqual([{ url: "/settings/privacy/profile_visibility/", body: "value=friends", reported: true }]);
    expect(selects("profile_visibility").map((s) => s.value)).toEqual(["friends", "friends"]);
    const buttons = selects("profile_visibility").map((s) => s.previousElementSibling);
    expect(buttons.map((b) => b?.getAttribute("title"))).toEqual(["Name, avatar & bio - Visible to: Friends Only - click to change", "Social - Visible to: Friends Only - click to change"]);
    expect(buttons.map((b) => b?.getAttribute("aria-label"))).toEqual(["Change who can see this - currently visible to: Friends Only", "Change who can see this - currently visible to: Friends Only"]);
    expect(buttons.map((b) => b?.querySelector("i")?.textContent)).toEqual(["lock", "lock"]);
    expect(selects("contact_visibility")[0]?.value).toBe("no_one");
    expect(toasts).toEqual(["success:Privacy setting updated."]);
    expect(selects("profile_visibility")[0]?.hidden).toBe(true);
});

test("anyone shows the open eye", async () => {
    respond = () => new Response(JSON.stringify({ ok: true, value: "anyone", display: "Anyone (Logged In)" }), { headers: { "Content-Type": "application/json" } });
    choose(selects("contact_visibility")[0], "anyone");
    await settle();
    expect(selects("contact_visibility")[0]?.previousElementSibling?.querySelector("i")?.textContent).toBe("visibility");
});

test("a refusal says why, once, and the picker goes back to what is saved", async () => {
    respond = () => new Response(JSON.stringify({ ok: false, error: "Turn on Community to choose who can see this." }), { status: 409, headers: { "Content-Type": "application/json" } });
    choose(selects("profile_visibility")[0], "no_one");
    await settle();
    expect(toasts).toEqual(["error:Turn on Community to choose who can see this."]);
    expect(selects("profile_visibility").map((s) => s.value)).toEqual(["anyone", "anyone"]);

    toasts = [];
    respond = () => Promise.reject(new TypeError("Failed to fetch"));
    choose(selects("profile_visibility")[0], "no_one");
    await settle();
    expect(toasts).toEqual(["error:Could not update privacy setting."]);
    expect(selects("profile_visibility")[0]?.value).toBe("anyone");
});
