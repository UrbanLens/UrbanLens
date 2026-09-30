import { afterEach, beforeAll, beforeEach, expect, test } from "bun:test";

import { installNotificationPrefs, type NotificationAccess } from "./notification-prefs";

const PREFS = `
  <div id="notif-prefs">
    <form>
      <input type="checkbox" class="notif-none-box" data-field="message">
      <input type="checkbox" class="notif-delivery-box" name="message__site" data-field="message" checked>
      <input type="checkbox" class="notif-delivery-box" name="message__email" data-field="message">
      <input type="checkbox" class="notif-none-box" data-field="trip">
      <input type="checkbox" class="notif-delivery-box" name="trip__site" data-field="trip" checked>
    </form>
    <div id="notif-browser-prefs" hidden>
      <p id="notif-browser-status"></p>
      <button type="button" id="notif-browser-enable" data-notif-action="enable-browser">Enable</button>
    </div>
  </div>`;

const realToastr = window.toastr;
let access: NotificationAccess;
let toasts: string[];

const box = (name: string) => document.querySelector<HTMLInputElement>(name.startsWith(".") || name.startsWith("[") ? name : `[name="${name}"]`);
const toggle = (el: HTMLInputElement | null) => {
    if (!el) throw new Error("no box");
    el.checked = !el.checked;
    el.dispatchEvent(new Event("change", { bubbles: true }));
};
const settle = async () => {
    for (let i = 0; i < 3; i++) await new Promise((resolve) => setTimeout(resolve, 0));
};

beforeAll(() => {
    installNotificationPrefs(() => access);
});

beforeEach(() => {
    toasts = [];
    access = { permission: "default", requestPermission: async () => "default" };
    window.toastr = Object.assign(Object.create(null), {
        success: (m: string) => toasts.push(`success:${m}`),
        error: (m: string) => toasts.push(`error:${m}`),
        info: (m: string) => toasts.push(`info:${m}`),
        warning: (m: string) => toasts.push(`warning:${m}`),
        clear: () => undefined,
    });
    document.body.innerHTML = PREFS;
});

afterEach(() => {
    window.toastr = realToastr;
});

test("None clears that event's deliveries, and a delivery clears None", () => {
    toggle(box('.notif-none-box[data-field="message"]'));
    expect([box("message__site")?.checked, box("message__email")?.checked]).toEqual([false, false]);
    expect(box("trip__site")?.checked).toBe(true);
    toggle(box("message__email"));
    expect(box('.notif-none-box[data-field="message"]')?.checked).toBe(false);
});

test("clearing the last delivery ticks None", () => {
    toggle(box("trip__site"));
    expect(box('.notif-none-box[data-field="trip"]')?.checked).toBe(true);
});

test("the browser section shows each permission state, and after every swap", async () => {
    document.body.dispatchEvent(new CustomEvent("htmx:afterSettle", { bubbles: true }));
    const status = () => document.getElementById("notif-browser-status")?.textContent;
    const button = () => document.getElementById("notif-browser-enable");
    expect(document.getElementById("notif-browser-prefs")?.hidden).toBe(false);
    expect([status(), button()?.hidden]).toEqual(["", false]);

    access = { permission: "denied", requestPermission: async () => "denied" };
    document.body.innerHTML = PREFS;
    document.body.dispatchEvent(new CustomEvent("htmx:afterSettle", { bubbles: true }));
    expect(status()).toContain("Blocked");
    expect(button()?.hidden).toBe(true);
});

test("enabling asks the browser, and says so when granted", async () => {
    let permission: NotificationPermission = "default";
    access = {
        get permission() {
            return permission;
        },
        requestPermission: async () => {
            permission = "granted";
            return permission;
        },
    };
    document.body.dispatchEvent(new CustomEvent("htmx:afterSettle", { bubbles: true }));
    document.getElementById("notif-browser-enable")?.click();
    await settle();
    expect(document.getElementById("notif-browser-status")?.textContent).toContain("Enabled");
    expect(document.getElementById("notif-browser-enable")?.hidden).toBe(true);
    expect(toasts).toEqual(["success:Browser notifications enabled."]);
});

test("without the Notification API the browser section stays hidden", () => {
    access = null;
    document.body.dispatchEvent(new CustomEvent("htmx:afterSettle", { bubbles: true }));
    expect(document.getElementById("notif-browser-prefs")?.hidden).toBe(true);
});
