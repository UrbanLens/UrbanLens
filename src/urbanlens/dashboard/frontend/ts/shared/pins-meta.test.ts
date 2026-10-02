import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { pinsMetaChecker } from "./pins-meta";
import { SESSION_ENDED_MESSAGE, wrapFetch } from "./site-runtime";

const realFetch = globalThis.fetch;
const realToastr = window.toastr;

let netReports: string[];
let toasts: string[];
let offline: boolean[];

/** Answers each call with the next outcome, through the fetch net every base.html page installs. */
function serve(...outcomes: Array<Response | "network-error">): void {
    let call = 0;
    const answer = async (): Promise<Response> => {
        const outcome = outcomes[Math.min(call++, outcomes.length - 1)]!;
        if (outcome === "network-error") throw new TypeError("Failed to fetch");
        return outcome;
    };
    globalThis.fetch = wrapFetch(Object.assign(answer, realFetch), (m) => void netReports.push(m));
}

beforeEach(() => {
    netReports = [];
    toasts = [];
    offline = [];
    window.toastr = {
        success: () => undefined,
        error: (m: string) => void toasts.push(m),
        warning: (m: string) => void toasts.push(m),
        info: () => undefined,
        clear: () => undefined,
    };
});

afterEach(() => {
    globalThis.fetch = realFetch;
    window.toastr = realToastr;
});

const check = (): ReturnType<typeof pinsMetaChecker> => pinsMetaChecker("/map/pins/meta/", (value) => void offline.push(value));

describe("the map's background check for pin changes", () => {
    test("an unreachable server shows the offline indicator on every tick, without a toast per tick", async () => {
        serve("network-error", new Response("", { status: 503 }));
        const tick = check();

        expect(await tick()).toBeNull();
        expect(await tick()).toBeNull();

        expect(offline).toEqual([true, true]);
        expect(netReports).toEqual([]);
        expect(toasts).toEqual([]);
    });

    test("an ended session is said once, not on every tick, and is not called offline", async () => {
        serve(new Response(SESSION_ENDED_MESSAGE, { status: 401 }));
        const tick = check();

        expect(await tick()).toBeNull();
        expect(await tick()).toBeNull();

        expect(toasts).toEqual([SESSION_ENDED_MESSAGE]);
        expect(offline).toEqual([false, false]);
        expect(netReports).toEqual([]);
    });

    test("an answer clears the indicator and hands back the stamp", async () => {
        serve(Response.json({ app_uuid: "a", fingerprint: "f1" }));

        expect(await check()()).toEqual({ app_uuid: "a", fingerprint: "f1" });
        expect(offline).toEqual([false]);
    });

    test("a session that ends again after a sign-in is said again", async () => {
        const ended = (): Response => new Response(SESSION_ENDED_MESSAGE, { status: 401 });
        serve(ended(), Response.json({ fingerprint: "f1" }), ended());
        const tick = check();

        await tick();
        await tick();
        await tick();

        expect(toasts).toEqual([SESSION_ENDED_MESSAGE, SESSION_ENDED_MESSAGE]);
    });
});
