import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { installSiteStatsPage } from "./site-stats-page";

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
const realFetch = globalThis.fetch;
const realToastr = window.toastr;
let toasts: string[] = [];
let scheduled: { ms: number; run: () => void }[] = [];
let reloads = 0;
let reported: unknown[] = [];
let respond: () => Response = () => new Response(JSON.stringify({ ok: true, changed: true, message: "Pulled 3 commits." }), { status: 200 });
let uninstall: () => void = () => {};

beforeEach(() => {
    toasts = [];
    scheduled = [];
    reloads = 0;
    reported = [];
    respond = () => new Response(JSON.stringify({ ok: true, changed: true, message: "Pulled 3 commits." }), { status: 200 });
    window.toastr = {
        success: (m: string) => void toasts.push(`success:${m}`),
        info: (m: string) => void toasts.push(`info:${m}`),
        warning: (m: string) => void toasts.push(`warning:${m}`),
        error: (m: string) => void toasts.push(`error:${m}`),
        clear: () => {},
    };
    globalThis.fetch = Object.assign(
        async (_input: RequestInfo | URL, init?: RequestInit) => {
            reported.push(init ? Reflect.get(init, "__ulReported") : undefined);
            return respond();
        },
        { preconnect: realFetch.preconnect },
    );
    document.body.innerHTML = `<span id="stats-refresh-badge"></span><div id="system"></div>`;
    uninstall = installSiteStatsPage(document, {
        reload: () => void reloads++,
        later: (ms, run) => void scheduled.push({ ms, run }),
    });
});

afterEach(() => {
    uninstall();
    globalThis.fetch = realFetch;
    window.toastr = realToastr;
});

function swapInPullButton(): HTMLButtonElement {
    const system = document.getElementById("system");
    if (!system) throw new Error("fixture");
    system.innerHTML = `<button id="git-pull-refresh-btn" data-url="/site-admin/pull/"><i>sync</i> Pull latest</button>`;
    system.dispatchEvent(new CustomEvent("htmx:afterSwap", { bubbles: true }));
    const button = document.getElementById("git-pull-refresh-btn");
    if (!(button instanceof HTMLButtonElement)) throw new Error("fixture");
    return button;
}

describe("the pull button", () => {
    test("works once swapped in, and reloads after saying what changed", async () => {
        const button = swapInPullButton();
        button.click();
        expect(button.disabled).toBe(true);
        expect(button.textContent).toContain("Pulling...");
        await settle();
        await settle();
        expect(toasts).toEqual(["info:Pulling latest code...", "success:Pulled 3 commits."]);
        expect(scheduled.map((s) => s.ms)).toContain(1200);
        for (const s of scheduled) s.run();
        expect(reloads).toBe(1);
    });

    test("a refusal puts the button back and says why", async () => {
        respond = () => new Response(JSON.stringify({ ok: false, message: "Working tree is dirty." }), { status: 409 });
        const button = swapInPullButton();
        button.click();
        await settle();
        await settle();
        expect(button.disabled).toBe(false);
        expect(button.innerHTML).toBe("<i>sync</i> Pull latest");
        expect(toasts).toContain("error:Working tree is dirty.");
        // Only its own message: the site's fetch net would add "Request failed (HTTP 409)." otherwise.
        expect(reported).toEqual([true]);
        expect(reloads).toBe(0);
    });

    test("a response that is not JSON is still a failure", async () => {
        respond = () => new Response("<h1>Bad Gateway</h1>", { status: 502, statusText: "Bad Gateway" });
        swapInPullButton().click();
        await settle();
        await settle();
        expect(toasts).toContain("error:Bad Gateway");
    });
});

test("each swap flashes the refresh badge", () => {
    swapInPullButton();
    expect(document.getElementById("stats-refresh-badge")?.classList.contains("stats-refresh-badge--active")).toBe(true);
    for (const s of scheduled) s.run();
    expect(document.getElementById("stats-refresh-badge")?.classList.contains("stats-refresh-badge--active")).toBe(false);
});
