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

import { getJson, postForm } from "./session-request";

const toasted: string[] = [];

mock.module("./dialogs", () => ({
    toast: {
        error: (message: string) => toasted.push(message),
        success: () => {},
        info: () => {},
        warning: () => {},
    },
}));

const realFetch = globalThis.fetch;

function respond(body: string, init: ResponseInit): void {
    globalThis.fetch = mock(async () => new Response(body, init)) as unknown as typeof fetch;
}

describe("session-request", () => {
    beforeEach(() => {
        toasted.length = 0;
    });

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

    test("a 204 is a success, not an empty-body failure", async () => {
        respond("", { status: 204 });

        expect(await postForm("/games/leave/", {})).toBeNull();
    });
});
