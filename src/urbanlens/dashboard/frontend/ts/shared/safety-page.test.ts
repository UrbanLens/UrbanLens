import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { installDeclarativeActions } from "./declarative-actions";
import { formatGraceHours, initArchiveUnlock, installCheckinTiming, installLiveLocationMarker, installSafetyPage, isLeavingAllowed, resetLeavingForTests, toLocalInputValue } from "./safety-page";

const realFetch = globalThis.fetch;
const realToastr = window.toastr;
const realConfirmDialog = window.confirmDialog;
let posts: string[] = [];
// The delete view redirects to the safety home page, which fetch follows.
const deletedResponse = (): Response => new Response("<!doctype html><title>Safety</title>", { status: 200, headers: { "Content-Type": "text/html" } });
let respond: () => Response = deletedResponse;
let toasts: string[] = [];
let answer = true;
let asked: string[] = [];

const settle = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

beforeAll(() => {
    globalThis.fetch = Object.assign(async (input: RequestInfo | URL) => {
        posts.push(String(input));
        return respond();
    }, realFetch);
    window.confirmDialog = async (options) => {
        asked.push(typeof options === "string" ? options : (options.title ?? ""));
        return answer;
    };
    // Core's data-confirm, which the resolve forms rely on, is registered first on every page.
    installDeclarativeActions();
    installSafetyPage();
    installSafetyPage();
});

afterAll(() => {
    globalThis.fetch = realFetch;
    window.toastr = realToastr;
    window.confirmDialog = realConfirmDialog;
});

beforeEach(() => {
    posts = [];
    toasts = [];
    asked = [];
    answer = true;
    respond = deletedResponse;
    window.toastr = { success: (m) => void toasts.push(`ok: ${m}`), error: (m) => void toasts.push(`error: ${m}`), warning: () => undefined, info: () => undefined, clear: () => undefined };
});

describe("formatGraceHours", () => {
    test("says hour or hours", () => {
        expect(formatGraceHours("1")).toBe("1 hour");
        expect(formatGraceHours("2")).toBe("2 hours");
        expect(formatGraceHours("2.5")).toBe("2.5 hours");
    });
});

describe("delete", () => {
    test("from the list, it removes the card once the server has", async () => {
        document.body.innerHTML = `<div id="card-1"><button type="button" class="safety-checkin-delete-btn" data-delete-url="/safety/1/delete/" data-card-id="card-1" data-contacts-notified="1">x</button></div>`;
        document.querySelector<HTMLElement>(".safety-checkin-delete-btn")?.click();
        await settle();
        expect(asked).toEqual(["Delete this check-in?"]);
        expect(posts).toEqual(["/safety/1/delete/"]);
        expect(document.getElementById("card-1")).toBeNull();
        expect(toasts).toEqual(["ok: Check-in deleted."]);
    });

    test("a refused delete keeps the card", async () => {
        respond = () => new Response("", { status: 403 });
        document.body.innerHTML = `<div id="card-1"><button type="button" class="safety-checkin-delete-btn" data-delete-url="/safety/1/delete/" data-card-id="card-1">x</button></div>`;
        document.querySelector<HTMLElement>(".safety-checkin-delete-btn")?.click();
        await settle();
        expect(document.getElementById("card-1")).not.toBeNull();
        expect(toasts).toEqual(["error: Could not delete this check-in."]);
    });

    test("a delete that fails keeps the leave-page warning", async () => {
        resetLeavingForTests();
        respond = () => new Response("", { status: 500 });
        document.body.innerHTML = `<button type="button" id="safety-delete-btn" data-delete-url="/safety/1/delete/" data-redirect-url="/safety/">x</button>`;
        document.getElementById("safety-delete-btn")?.click();
        await settle();
        expect(toasts).toEqual(["error: Could not delete this check-in."]);
        expect(isLeavingAllowed()).toBe(false);
    });

    test("declining sends nothing", async () => {
        answer = false;
        document.body.innerHTML = `<button type="button" id="safety-delete-btn" data-delete-url="/safety/1/delete/" data-redirect-url="/safety/">x</button>`;
        document.getElementById("safety-delete-btn")?.click();
        await settle();
        expect(posts).toEqual([]);
    });
});

describe("resolving", () => {
    let submits = 0;
    // After the document listeners, standing in for the navigation.
    const recordSubmit = (event: Event): void => {
        if (event.defaultPrevented) return;
        submits++;
        event.preventDefault();
    };

    beforeAll(() => window.addEventListener("submit", recordSubmit));
    afterAll(() => window.removeEventListener("submit", recordSubmit));
    beforeEach(() => {
        submits = 0;
        resetLeavingForTests();
    });

    function form(attrs: string): HTMLFormElement {
        document.body.innerHTML = `<form data-safety-resolve ${attrs} action="/safety/1/cancel/" method="post"><button type="submit">Go</button></form>`;
        const el = document.querySelector("form");
        if (!el) throw new Error("no form");
        return el;
    }

    test("a declined confirm stops the submit, and the warning stays", async () => {
        answer = false;
        form('data-confirm="Cancel this check-in?"').requestSubmit();
        await settle();
        expect(asked).toEqual(["Cancel this check-in?"]);
        expect(submits).toBe(0);
        expect(isLeavingAllowed()).toBe(false);
    });

    test("a confirmed resolve submits and lets the page go", async () => {
        form('data-confirm="Cancel this check-in?"').requestSubmit();
        await settle();
        expect(submits).toBe(1);
        expect(isLeavingAllowed()).toBe(true);
    });

    test("one with nothing to confirm lets the page go at once", () => {
        form("").requestSubmit();
        expect(submits).toBe(1);
        expect(isLeavingAllowed()).toBe(true);
    });
});

describe("live updates", () => {
    test("a status change updates the badge and hides the resolve buttons", () => {
        document.body.innerHTML = `<span id="safety-status-badge" class="safety-checkin-status-badge safety-checkin-status-badge--active">Active</span><div id="safety-resolve-actions"></div>`;
        document.body.dispatchEvent(new CustomEvent("safetyStatusUpdate", { detail: { status: "resolved", status_display: "Resolved", is_resolved: true } }));
        const badge = document.getElementById("safety-status-badge");
        expect(badge?.className).toBe("safety-checkin-status-badge safety-checkin-status-badge--resolved");
        expect(badge?.textContent).toBe("Resolved");
        expect(document.getElementById("safety-resolve-actions")?.hidden).toBe(true);
    });

    test("the last known position shows on load, and an ended share says so", () => {
        document.body.innerHTML = `<div id="safety-live-location-card" data-live-lat="42.5" data-live-lng="-73.5" data-live-updated-at="not a date"><p id="safety-live-location-status" data-idle-text="owl is not sharing their live location."></p></div>`;
        const card = document.getElementById("safety-live-location-card");
        if (!card) throw new Error("no card");
        installLiveLocationMarker(card);
        const status = document.getElementById("safety-live-location-status");
        expect(status?.textContent).toBe("Live location updated just now.");
        document.body.dispatchEvent(new CustomEvent("safetyLocationUpdate", { detail: { latitude: null, longitude: null, updated_at: null } }));
        expect(status?.textContent).toBe("owl is not sharing their live location.");
    });

    test("with no stored position, the status keeps its own words", () => {
        document.body.innerHTML = `<div id="safety-live-location-card" data-live-lat="" data-live-lng=""><p id="safety-live-location-status" data-idle-text="Only partners can see this.">Only partners can see this.</p></div>`;
        const card = document.getElementById("safety-live-location-card");
        if (!card) throw new Error("no card");
        installLiveLocationMarker(card);
        expect(document.getElementById("safety-live-location-status")?.textContent).toBe("Only partners can see this.");
    });
});

describe("archive unlock", () => {
    test("sets up the encryption client with the page's details before decrypting", async () => {
        const calls: unknown[][] = [];
        const stub = {
            init: (cfg: unknown) => void calls.push(["init", cfg]),
            decryptSafetyArchive: async (...args: string[]) => {
                calls.push(["decrypt", ...args]);
                return { title: "Night walk", plan_details: "", resolved_by_label: "you" };
            },
        };
        const real = Object.getOwnPropertyDescriptor(window, "UrbanLensE2EE");
        Object.defineProperty(window, "UrbanLensE2EE", { value: stub, configurable: true, writable: true });
        document.body.innerHTML = `
          <button type="button" id="safety-archive-unlock-btn" data-ciphertext="c" data-nonce="n" data-sealed-key="k"
                  data-self-slug="owl" data-login-identifier="owl" data-url-login-params="/e2ee/login-params/" data-url-keys="/e2ee/keys/">Unlock</button>
          <dl id="safety-archive-unlocked" hidden><dd id="safety-archive-title"></dd><dd id="safety-archive-plan"></dd><dd id="safety-archive-resolved-by"></dd></dl>`;
        try {
            initArchiveUnlock();
            document.getElementById("safety-archive-unlock-btn")?.click();
            await settle();
        } finally {
            if (real) Object.defineProperty(window, "UrbanLensE2EE", real);
            else Reflect.deleteProperty(window, "UrbanLensE2EE");
        }
        expect(calls.map((c) => c[0])).toEqual(["init", "decrypt"]);
        expect(calls[0]?.[1]).toMatchObject({ selfSlug: "owl", loginIdentifier: "owl", urls: { loginParams: "/e2ee/login-params/", keys: "/e2ee/keys/" } });
        expect(calls[1]?.slice(1)).toEqual(["k", "c", "n"]);
        expect(document.getElementById("safety-archive-title")?.textContent).toBe("Night walk");
        expect(document.getElementById("safety-archive-plan")?.textContent).toBe("(no plan recorded)");
        expect(document.getElementById("safety-archive-unlocked")?.hidden).toBe(false);
    });
});

describe("create form timing", () => {
    function render(): { form: HTMLFormElement; picker: HTMLInputElement; utc: HTMLInputElement } {
        document.body.innerHTML = `<form id="f"><input type="datetime-local" id="by"><input type="hidden" id="utc">
          <div class="safety-grace-slider"><input type="range" data-role="range" value="2"></div><p id="safety-grace-end-hint"></p></form>`;
        const form = document.getElementById("f");
        const picker = document.getElementById("by");
        const utc = document.getElementById("utc");
        if (!(form instanceof HTMLFormElement) || !(picker instanceof HTMLInputElement) || !(utc instanceof HTMLInputElement)) throw new Error("bad fixture");
        installCheckinTiming(form, picker, utc);
        return { form, picker, utc };
    }

    test("defaults an hour out and mirrors the time as UTC", () => {
        const { picker, utc } = render();
        const picked = new Date(picker.value).getTime();
        expect(picked - Date.now()).toBeGreaterThan(58 * 60000);
        expect(new Date(utc.value).getTime()).toBe(picked);
        expect(document.getElementById("safety-grace-end-hint")?.textContent).toStartWith("Contacts will be notified at ");
    });

    test("a time in the past does not submit", () => {
        const { form, picker } = render();
        picker.value = toLocalInputValue(new Date(Date.now() - 3600000));
        const event = new Event("submit", { cancelable: true });
        form.dispatchEvent(event);
        expect(event.defaultPrevented).toBe(true);
        expect(picker.validationMessage).toBe("Expected check-in time must be in the future.");
    });
});
