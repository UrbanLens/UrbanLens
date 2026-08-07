import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { HttpError, fetchJson, sendJson } from "./fetch-json";

const realFetch = globalThis.fetch;
let calls: { url: string; init: RequestInit }[] = [];

interface StubResponse {
    status?: number;
    body?: string;
    json?: unknown;
}

function stub({ status = 200, body, json }: StubResponse = {}): void {
    calls = [];
    const text = body ?? (json === undefined ? "" : JSON.stringify(json));
    globalThis.fetch = ((url: string, init: RequestInit) => {
        calls.push({ url: String(url), init });
        return Promise.resolve({
            ok: status >= 200 && status < 300,
            status,
            text: () => Promise.resolve(text),
            json: () => (text ? Promise.resolve(JSON.parse(text)) : Promise.reject(new SyntaxError("Unexpected end of JSON input"))),
        } as Response);
    }) as unknown as typeof fetch;
}

beforeEach(() => {
    stub({ json: { ok: true } });
    window.csrftoken = "tok123";
});

afterEach(() => {
    globalThis.fetch = realFetch;
});

describe("a successful request", () => {
    test("returns the parsed body", async () => {
        stub({ json: { rating: 4 } });
        expect(await fetchJson<{ rating: number }>("/x/")).toEqual({ rating: 4 });
    });

    test("a 204 returns null rather than throwing on an empty body", async () => {
        // Calling .json() on a 204 throws, which would turn a recorded position or
        // a DRF delete into an apparent failure.
        stub({ status: 204 });
        expect(await fetchJson("/x/")).toBeNull();
    });
});

describe("a rejected request", () => {
    // The whole point: fetch resolves for these, so without an explicit check the
    // caller runs its success path and tells the user the write landed.
    for (const status of [400, 401, 403, 404, 409, 500, 503]) {
        test(`${status} throws`, async () => {
            stub({ status, json: {} });
            await expect(fetchJson("/x/")).rejects.toThrow();
        });
    }

    test("the error carries the status", async () => {
        stub({ status: 403, json: {} });
        try {
            await fetchJson("/x/");
            throw new Error("should have thrown");
        } catch (error) {
            expect(error).toBeInstanceOf(HttpError);
            expect((error as HttpError).status).toBe(403);
        }
    });

    test("it uses DRF's detail field, so the toast can be specific", async () => {
        stub({ status: 400, json: { detail: "You already rated this pin." } });
        await expect(fetchJson("/x/")).rejects.toThrow("You already rated this pin.");
    });

    test("it uses this project's error field", async () => {
        stub({ status: 400, json: { error: "Sharing is disabled." } });
        await expect(fetchJson("/x/")).rejects.toThrow("Sharing is disabled.");
    });

    test("it uses a message field", async () => {
        stub({ status: 400, json: { message: "Nope." } });
        await expect(fetchJson("/x/")).rejects.toThrow("Nope.");
    });

    test("an HTML error page falls back to the status, not a page of markup", async () => {
        stub({ status: 500, body: "<!doctype html><title>Server Error</title>" });
        await expect(fetchJson("/x/")).rejects.toThrow("HTTP 500");
    });

    test("an empty error body falls back to the status", async () => {
        stub({ status: 502 });
        await expect(fetchJson("/x/")).rejects.toThrow("HTTP 502");
    });

    test("a JSON body with no recognised field falls back to the status", async () => {
        stub({ status: 400, json: { unexpected: true } });
        await expect(fetchJson("/x/")).rejects.toThrow("HTTP 400");
    });
});

describe("a request that never completes", () => {
    test("a network failure propagates", async () => {
        globalThis.fetch = (() => Promise.reject(new Error("offline"))) as unknown as typeof fetch;
        await expect(fetchJson("/x/")).rejects.toThrow("offline");
    });

    test("it is abandoned after the timeout", async () => {
        globalThis.fetch = ((_url: string, init: RequestInit) =>
            new Promise((_resolve, reject) => {
                init.signal?.addEventListener("abort", () => reject(new Error("aborted")));
            })) as unknown as typeof fetch;

        await expect(fetchJson("/x/", { timeoutMs: 20 })).rejects.toThrow("aborted");
    });

    test("a request that finishes in time is not aborted afterwards", async () => {
        stub({ json: { ok: true } });
        expect(await fetchJson<{ ok: boolean }>("/x/", { timeoutMs: 50 })).toEqual({ ok: true });
        await new Promise((resolve) => setTimeout(resolve, 80));
        // Nothing to assert beyond not throwing: the timer must have been cleared.
    });
});

describe("sendJson", () => {
    test("sends the method, JSON body and content type", async () => {
        await sendJson("/pins/1/", "PATCH", { rating: 5 });

        expect(calls[0]?.init.method).toBe("PATCH");
        expect(calls[0]?.init.body).toBe('{"rating":5}');
        expect((calls[0]?.init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    });

    test("includes the CSRF token Django requires", async () => {
        await sendJson("/pins/1/", "POST", {});
        expect((calls[0]?.init.headers as Record<string, string>)["X-CSRFToken"]).toBe("tok123");
    });

    test("callers can add their own headers without losing the CSRF one", async () => {
        await sendJson("/pins/1/", "POST", {}, { headers: { "X-Requested-With": "XMLHttpRequest" } });

        const headers = calls[0]?.init.headers as Record<string, string>;
        expect(headers["X-Requested-With"]).toBe("XMLHttpRequest");
        expect(headers["X-CSRFToken"]).toBe("tok123");
    });

    test("omits the body entirely when there is none", async () => {
        await sendJson("/pins/1/", "DELETE");
        expect(calls[0]?.init.body).toBeUndefined();
    });

    test("a rejected write throws rather than resolving", async () => {
        stub({ status: 403, json: { detail: "Not yours." } });
        await expect(sendJson("/pins/1/", "PATCH", { name: "x" })).rejects.toThrow("Not yours.");
    });

    test("a 204 write resolves with null", async () => {
        stub({ status: 204 });
        expect(await sendJson("/pins/1/", "DELETE")).toBeNull();
    });
});
