/**
 * The Organize prewarm chain loads each deferred tab once (P191): a tab its own trigger already loaded is
 * skipped, and a tab the chain loaded refuses its own trigger when it is later shown.
 */

import { afterEach, beforeEach, expect, test } from "bun:test";

import type { HtmxApi } from "../types/globals";
import { installOrgTabPrewarm } from "./organize-header";

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function panel(kind: string): string {
    return `<div class="organize-panel" id="panel-${kind}" hidden hx-get="/${kind}/rows/" hx-trigger="intersect once"
        hx-target="#${kind}-rows" hx-swap="innerHTML"><div id="${kind}-rows"></div></div>`;
}

let requested: string[];

beforeEach(() => {
    requested = [];
    document.body.innerHTML = panel("category") + panel("status") + panel("people");
    const htmx: HtmxApi = {
        process: () => {},
        trigger: () => {},
        ajax: async (_verb, url, options) => {
            requested.push(url);
            const target = document.querySelector<HTMLElement>(String(options.target));
            await tick();
            target?.dispatchEvent(new CustomEvent("htmx:afterSwap", { bubbles: true, detail: { target } }));
        },
    };
    window.htmx = htmx;
});

afterEach(() => {
    delete window.htmx;
});

/** What htmx dispatches on a panel when its own `hx-trigger` fires; false when a listener cancelled it. */
function ownTrigger(kind: string): boolean {
    const el = document.getElementById(`panel-${kind}`)!;
    return el.dispatchEvent(new CustomEvent("htmx:confirm", { bubbles: true, cancelable: true, detail: { elt: el } }));
}

function ownRequestStarts(kind: string): void {
    const el = document.getElementById(`panel-${kind}`)!;
    el.dispatchEvent(new CustomEvent("htmx:beforeRequest", { bubbles: true, detail: { elt: el } }));
}

test("the chain loads every hidden tab once, in order", async () => {
    installOrgTabPrewarm();
    for (let i = 0; i < 6; i++) await tick();

    expect(requested).toEqual(["/category/rows/", "/status/rows/", "/people/rows/"]);
});

test("a tab the chain loaded refuses its own trigger when it is shown", async () => {
    installOrgTabPrewarm();
    for (let i = 0; i < 6; i++) await tick();

    expect(ownTrigger("status")).toBe(false);
});

test("a tab the chain has not reached keeps its own trigger", () => {
    window.htmx = { process: () => {}, trigger: () => {}, ajax: () => new Promise<void>(() => {}) };
    installOrgTabPrewarm();

    expect(ownTrigger("people")).toBe(true);
});

test("a tab opened before the chain reached it is not loaded again", async () => {
    let release: () => void = () => {};
    const firstSwap = new Promise<void>((resolve) => {
        release = resolve;
    });
    const htmx = window.htmx!;
    window.htmx = {
        ...htmx,
        ajax: async (verb, url, options) => {
            if (url === "/category/rows/") await firstSwap;
            await htmx.ajax(verb, url, options);
        },
    };
    installOrgTabPrewarm();
    ownRequestStarts("status");
    release();
    for (let i = 0; i < 8; i++) await tick();

    expect(requested).toEqual(["/category/rows/", "/people/rows/"]);
});
