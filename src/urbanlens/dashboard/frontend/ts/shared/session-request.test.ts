/**
 * The helpers exist to check what the three copies they replaced did not.
 *
 * `fetch` resolves for a 400 or a 500 - it only rejects when the request never
 * completed - so the old `postForm`/`getJson` ran `response.json()` against
 * whatever the error page produced. Usually that is a `SyntaxError` thrown
 * inside a `void`-ed promise: a page that quietly stops working, with nothing in
 * the console a user would see (P11).
 *
 * So the tests that matter are the refusal ones, and each asserts the *shape*
 * the call sites already read (`{ error }`) rather than that "something was
 * returned" - a helper that swallowed the failure and returned `{}` would pass
 * a looser assertion and leave every `if (response.error)` as dead code.
 */

import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";

import { getJson, postForm, postMultipart } from "./session-request";

const toasted: string[] = [];

// Observed through `window.toastr`, which is the seam `toast` itself routes
// through, rather than through `mock.module("./dialogs")`. A module mock in bun
// is installed on the process registry and is never taken down, so every file
// that runs after this one in the same `bun test` invocation imports the stub -
// including dialogs.test.ts, whose whole subject is the real implementation.
// This observes the same messages and exercises the real path.
function collectToasts(): void {
    (window as { toastr?: unknown }).toastr = {
        error: (message: string) => toasted.push(message),
        success: () => {},
        info: () => {},
        warning: () => {},
    };
}

function stopCollectingToasts(): void {
    delete (window as { toastr?: unknown }).toastr;
}

const realFetch = globalThis.fetch;

function respond(body: string, init: ResponseInit): void {
    globalThis.fetch = mock(async () => new Response(body, init)) as unknown as typeof fetch;
}

describe("session-request", () => {
    beforeEach(() => {
        toasted.length = 0;
        collectToasts();
    });

    afterEach(stopCollectingToasts);

    afterEach(() => {
        globalThis.fetch = realFetch;
    });

    test("postForm returns the parsed body on success", async () => {
        respond(JSON.stringify({ session_id: 7 }), { status: 200, headers: { "Content-Type": "application/json" } });

        expect(await postForm("/games/start/", { difficulty: "easy" })).toEqual({ session_id: 7 });
    });

    test("postForm sends the fields form-encoded", async () => {
        const calls: [string, RequestInit][] = [];
        globalThis.fetch = mock(async (url: string, init: RequestInit) => {
            calls.push([url, init]);
            return new Response("{}", { status: 200 });
        }) as unknown as typeof fetch;

        await postForm("/games/start/", { difficulty: "hard", total_rounds: "5" });

        const [url, init] = calls[0]!;
        expect(url).toBe("/games/start/");
        expect(init.method).toBe("POST");
        expect(String(init.body)).toBe("difficulty=hard&total_rounds=5");
    });

    test("postForm accepts a URLSearchParams, for repeated fields", async () => {
        // Invites send invite_profile_ids more than once, which a plain object
        // cannot express.
        const calls: RequestInit[] = [];
        globalThis.fetch = mock(async (_url: string, init: RequestInit) => {
            calls.push(init);
            return new Response("{}", { status: 200 });
        }) as unknown as typeof fetch;
        const params = new URLSearchParams();
        params.append("invite_profile_ids", "1");
        params.append("invite_profile_ids", "2");

        await postForm("/games/start/", params);

        expect(String(calls[0]!.body)).toBe("invite_profile_ids=1&invite_profile_ids=2");
    });

    test("a refused post comes back as an error, in the shape callers read", async () => {
        // The defect: the old helper called .json() on this and threw a
        // SyntaxError into a promise nobody was awaiting.
        respond("Not your turn.", { status: 403 });

        const result = await postForm("/games/answer/", {});

        expect(result.error).toBe("Not your turn.");
    });

    test("a JSON refusal keeps the server's own message", async () => {
        respond(JSON.stringify({ error: "That session has ended." }), { status: 400, headers: { "Content-Type": "application/json" } });

        expect((await postForm("/games/answer/", {})).error).toBe("That session has ended.");
    });

    test("an HTML error page is not put in front of the user", async () => {
        respond("<!doctype html><title>Server Error</title>", { status: 500 });

        expect((await postForm("/games/answer/", {})).error).toBe("HTTP 500");
    });

    test("postForm does not toast, because its callers already do", async () => {
        respond("Not your turn.", { status: 403 });

        await postForm("/games/answer/", {});

        expect(toasted).toEqual([]);
    });

    test("getJson returns the parsed body on success", async () => {
        respond(JSON.stringify({ friends: [{ profile_id: 3 }] }), { status: 200, headers: { "Content-Type": "application/json" } });

        expect(await getJson("/games/friends/")).toEqual({ friends: [{ profile_id: 3 }] });
    });

    test("a failed getJson tells the user, because no caller checks", async () => {
        // Every one of the 33 call sites reads `data.friends ?? []`, so a silent
        // failure renders an empty list and says nothing.
        respond("Session not found.", { status: 404 });

        const result = await getJson("/games/lobby/9/");

        expect(result.error).toBe("Session not found.");
        expect(toasted).toEqual(["Session not found."]);
    });

    test("a request that never completes is reported, not left hanging", async () => {
        globalThis.fetch = mock(async () => {
            throw new TypeError("Failed to fetch");
        }) as unknown as typeof fetch;

        expect((await postForm("/games/answer/", {})).error).toBe("Failed to fetch");
        expect((await getJson("/games/lobby/9/")).error).toBe("Failed to fetch");
    });

    test("a timeout says so in words a user can act on", async () => {
        globalThis.fetch = mock(async () => {
            throw new DOMException("The operation was aborted.", "AbortError");
        }) as unknown as typeof fetch;

        expect((await postForm("/games/answer/", {})).error).toContain("took too long");
    });

    test("a 204 resolves to an object, because every caller reads a property off it", async () => {
        // fetchJson answers a bodyless success with null, correctly. Passing that
        // through would make `if (response.error)` a TypeError - a crash where the
        // old code merely did nothing. No games endpoint answers 204 today;
        // turning one into a bodyless delete must not be what breaks its caller.
        respond("", { status: 204 });

        const result = await postForm("/games/leave/", {});

        expect(result).toEqual({});
        expect(result.error).toBeUndefined();
    });

    test("postMultipart returns the parsed body on success", async () => {
        respond(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json" } });

        const data = new FormData();
        data.append("image", new Blob(["x"]), "photo.jpg");

        expect(await postMultipart("/games/consensus/photo/", data)).toEqual({ ok: true });
    });

    test("postMultipart sends the FormData body with no explicit Content-Type", async () => {
        // A FormData body must reach fetch() with no Content-Type header at
        // all - the browser sets one itself (with the multipart boundary the
        // server needs) only when the header is absent. Regression guard: the
        // handwritten fetch() this replaced got this right by omission; a
        // careless refactor could easily add one back.
        const calls: [string, RequestInit][] = [];
        globalThis.fetch = mock(async (url: string, init: RequestInit) => {
            calls.push([url, init]);
            return new Response("{}", { status: 200 });
        }) as unknown as typeof fetch;
        const data = new FormData();
        data.append("image", new Blob(["x"]), "photo.jpg");

        await postMultipart("/games/consensus/photo/", data);

        const [url, init] = calls[0]!;
        expect(url).toBe("/games/consensus/photo/");
        expect(init.method).toBe("POST");
        expect(init.body).toBe(data);
        expect((init.headers as Record<string, string>)["Content-Type"]).toBeUndefined();
    });

    test("a refused upload comes back as an error, in the shape the caller reads", async () => {
        respond(JSON.stringify({ error: "That photo is too large." }), { status: 413, headers: { "Content-Type": "application/json" } });

        const data = new FormData();
        data.append("image", new Blob(["x"]), "photo.jpg");

        expect((await postMultipart("/games/consensus/photo/", data)).error).toBe("That photo is too large.");
    });

    test("a non-JSON refusal does not throw an uncaught SyntaxError", async () => {
        // The defect this replaces: `await response.json()` ran unconditionally,
        // before the ok check, so any non-JSON error body (a session-expiry
        // redirect to the login page, an nginx/proxy limit page) threw inside
        // an un-awaited async function - no toast, nothing in the console.
        respond("<!doctype html><title>413 Request Entity Too Large</title>", { status: 413 });

        const data = new FormData();
        data.append("image", new Blob(["x"]), "photo.jpg");

        expect((await postMultipart("/games/consensus/photo/", data)).error).toBe("HTTP 413");
    });

    test("postMultipart does not toast, because its caller already does", async () => {
        respond("Not your turn.", { status: 403 });
        const data = new FormData();
        data.append("image", new Blob(["x"]), "photo.jpg");

        await postMultipart("/games/consensus/photo/", data);

        expect(toasted).toEqual([]);
    });

    test("both helpers suppress the generic toast, because they report themselves", async () => {
        // base.html wraps window.fetch and toasts "Request failed (HTTP 503)."
        // for any non-2xx. These two say something better, so they opt out - and
        // opting out is per-call, not something fetchJson does for everyone.
        const inits: RequestInit[] = [];
        globalThis.fetch = mock(async (_url: string, init: RequestInit) => {
            inits.push(init);
            return new Response("{}", { status: 200 });
        }) as unknown as typeof fetch;

        await postForm("/games/start/", {});
        await getJson("/games/friends/");

        for (const init of inits) {
            expect((init as { __ulReported?: boolean }).__ulReported).toBe(true);
        }
    });
});
