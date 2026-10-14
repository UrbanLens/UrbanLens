import { afterAll, afterEach, beforeEach, expect, jest, test } from "bun:test";

import { installSubscriptionsPage } from "./subscriptions-page";

const FORM = (group: string, role: string) => `
  <li class="subscription-list__item">
    <form class="inline-sub-form" data-field-group="${group}" data-role="${role}">
      <input type="number" name="storage_quota_gb">
      <span class="save-status" aria-live="polite" aria-atomic="true"></span>
    </form>
  </li>`;

let uninstall: () => void = () => {};

beforeEach(() => {
    jest.useFakeTimers();
    uninstall();
    document.body.innerHTML = `<ul>${FORM("role_quota", "pro")}${FORM("role_email_limits", "pro")}${FORM("default_features", "__default__")}</ul>`;
    uninstall = installSubscriptionsPage(document);
});

afterEach(() => jest.useRealTimers());
afterAll(() => uninstall());

function form(group: string, role: string): HTMLFormElement {
    const el = Array.from(document.querySelectorAll<HTMLFormElement>(".inline-sub-form")).find((f) => f.dataset.fieldGroup === group && f.dataset.role === role);
    if (!el) throw new Error(`${group}/${role}`);
    return el;
}

function htmx(target: HTMLFormElement, name: string, status?: number): void {
    target.dispatchEvent(new CustomEvent(name, { bubbles: true, detail: { elt: target, xhr: { status } } }));
}

function status(target: HTMLFormElement): string {
    const el = target.querySelector(".save-status");
    return `${el?.className}|${el?.textContent}`;
}

const saved = (group: string, role: string) => document.body.dispatchEvent(new CustomEvent("roleSettingsSaved", { detail: { field_group: group, role } }));

test("a save shows progress on its own form, then Saved when the server confirms that form", () => {
    const quota = form("role_quota", "pro");
    htmx(quota, "htmx:beforeRequest");
    expect(status(quota)).toBe("save-status is-saving|Saving...");
    expect(status(form("role_email_limits", "pro"))).toBe("save-status|");
    htmx(quota, "htmx:afterRequest", 204);
    expect(status(quota)).toBe("save-status is-saving|Saving...");
    saved("role_quota", "pro");
    expect(status(quota)).toBe("save-status is-saved|Saved");
    expect(status(form("role_email_limits", "pro"))).toBe("save-status|");
});

test("a rejected or unsent save says so", () => {
    const quota = form("role_quota", "pro");
    htmx(quota, "htmx:beforeRequest");
    htmx(quota, "htmx:afterRequest", 400);
    expect(status(quota)).toBe("save-status is-error|Save failed (400)");
    htmx(quota, "htmx:afterRequest", 0);
    expect(status(quota)).toBe("save-status is-error|Save failed (?)");
});

test("an edit the form's own constraints refuse is reported as not saved", () => {
    const quota = form("role_quota", "pro");
    quota.dispatchEvent(new CustomEvent("htmx:validation:halted", { bubbles: true, detail: { elt: quota } }));
    expect(status(quota)).toBe("save-status is-error|Not saved");
});

test("the everyone row's features confirm like any role's", () => {
    saved("default_features", "__default__");
    expect(status(form("default_features", "__default__"))).toBe("save-status is-saved|Saved");
});

test("the outcome fades after a moment, and a new save restarts it", () => {
    const quota = form("role_quota", "pro");
    saved("role_quota", "pro");
    jest.advanceTimersByTime(2000);
    htmx(quota, "htmx:beforeRequest");
    jest.advanceTimersByTime(5000);
    expect(status(quota)).toBe("save-status is-saving|Saving...");
    saved("role_quota", "pro");
    jest.advanceTimersByTime(2600);
    expect(quota.querySelector(".save-status")?.classList.contains("is-fading")).toBe(true);
    jest.advanceTimersByTime(600);
    expect(quota.querySelector(".save-status")?.className).toBe("save-status");
});

test("requests from other forms leave the statuses alone", () => {
    document.body.insertAdjacentHTML("beforeend", `<form id="other"><span class="save-status"></span></form>`);
    const other = document.getElementById("other");
    other?.dispatchEvent(new CustomEvent("htmx:beforeRequest", { bubbles: true, detail: { xhr: { status: 0 } } }));
    expect(other?.querySelector(".save-status")?.className).toBe("save-status");
});
