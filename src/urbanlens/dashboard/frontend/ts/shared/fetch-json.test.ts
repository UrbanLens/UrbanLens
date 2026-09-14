import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { HttpError, fetchJson, fetchText, sendForText, sendJson } from "./fetch-json";

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

    // Many of this project's views answer a refused write with a bare HttpResponse("...", status=400) rather than JSON.
    test("a plain-text refusal is the message", async () => {
        stub({ status: 400, body: "Select at most 500 pins at a time." });
        await expect(fetchJson("/x/")).rejects.toThrow("Select at most 500 pins at a time.");
    });

    test("surrounding whitespace is trimmed off a plain-text refusal", async () => {
        stub({ status: 400, body: "  No pins specified.\t" });
        await expect(fetchJson("/x/")).rejects.toThrow("No pins specified.");
    });

    test("a multi-line plain-text body falls back to the status", async () => {
        stub({ status: 400, body: "Traceback (most recent call last):\n  File ..." });
        await expect(fetchJson("/x/")).rejects.toThrow("HTTP 400");
    });

    test("an over-long plain-text body falls back to the status", async () => {
        stub({ status: 400, body: "x".repeat(400) });
        await expect(fetchJson("/x/")).rejects.toThrow("HTTP 400");
    });

    test("a bare-fragment HTML body still falls back, even when short", async () => {
        stub({ status: 500, body: "<h1>Server Error (500)</h1>" });
        await expect(fetchJson("/x/")).rejects.toThrow("HTTP 500");
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

/**
 * The marker is a contract with a template, held together by a string.
 */
describe("the __ulReported contract with base.html", () => {
    const template = readFileSync(join(import.meta.dir, "../../../templates/dashboard/themes/base.html"), "utf8");

    test("the template still wraps window.fetch", () => {
        // If this goes, the marker is harmless but pointless, and the ~90
        // unmigrated call sites have lost their net.
        expect(template).toContain("__urbanLensWrapped");
    });

    test("the wrapper reads the same flag this module writes", () => {
        expect(template).toContain("init.__ulReported");
        expect(readFileSync(join(import.meta.dir, "fetch-json.ts"), "utf8")).toContain("__ulReported: reportsItsOwnErrors");
    });

    test("an ordinary caller keeps the net", async () => {
        // The regression this replaced: setting the marker inside fetchJson for everyone turned the generic toast into silence on every page.
        const inits: RequestInit[] = [];
        const real = globalThis.fetch;
        globalThis.fetch = (async (_url: string, init: RequestInit) => {
            inits.push(init);
            return new Response("{}", { status: 200 });
        }) as unknown as typeof fetch;
        try {
            await fetchJson("/anything/");
        } finally {
            globalThis.fetch = real;
        }

        expect((inits[0] as { __ulReported?: boolean }).__ulReported).toBe(false);
    });

    test("a caller that says so opts out", async () => {
        const inits: RequestInit[] = [];
        const real = globalThis.fetch;
        globalThis.fetch = (async (_url: string, init: RequestInit) => {
            inits.push(init);
            return new Response("{}", { status: 200 });
        }) as unknown as typeof fetch;
        try {
            await fetchJson("/anything/", { reportsItsOwnErrors: true });
        } finally {
            globalThis.fetch = real;
        }

        expect((inits[0] as { __ulReported?: boolean }).__ulReported).toBe(true);
    });

    test("the wrapper stays quiet on both failure paths, not just the non-2xx one", () => {
        // A timeout aborts, which lands in the catch rather than the then - and
        // fetchJson's own timeout is exactly the case that would double-toast.
        const wrapper = template.slice(template.indexOf("var wrappedFetch"), template.indexOf("wrappedFetch.__urbanLensWrapped"));
        expect(wrapper).toContain("!response.ok && !reported");
        expect(wrapper).toContain("if (!reported) {");
    });
});

describe("an endpoint that answers with markup", () => {
    // Organize's bulk delete/edit/merge answer with the re-rendered row list.
    test("fetchText returns the body verbatim rather than parsing it", async () => {
        stub({ body: "<li class=\"tag-card\">Bridges</li>" });
        expect(await fetchText("/rows/")).toBe('<li class="tag-card">Bridges</li>');
    });

    test("an empty fragment is not an error", async () => {
        // A row list with nothing left in it renders as nothing, and swapping
        // that in is the correct outcome of deleting the last row.
        stub({ body: "" });
        expect(await fetchText("/rows/")).toBe("");
    });

    test("a non-2xx still throws, carrying the server's own sentence", async () => {
        stub({ status: 400, body: "You cannot delete a protected label." });
        await expect(fetchText("/rows/")).rejects.toThrow("You cannot delete a protected label.");
    });

    test("an HTML error page is still discarded, even though the caller wants HTML", async () => {
        // The caller wanting markup back does not make Django's debug page a
        // sensible toast - and swapping it into the row list would be worse.
        stub({ status: 500, body: "<!doctype html><title>Server Error</title>" });
        await expect(fetchText("/rows/")).rejects.toThrow("HTTP 500");
    });

    test("sendForText sends JSON with the CSRF header and hands back markup", async () => {
        stub({ body: "<li>ok</li>" });
        expect(await sendForText("/rows/", "POST", { ids: [1, 2] })).toBe("<li>ok</li>");
        expect(calls[0]!.init.method).toBe("POST");
        expect(calls[0]!.init.body).toBe('{"ids":[1,2]}');
        expect((calls[0]!.init.headers as Record<string, string>)["X-CSRFToken"]).toBe("tok123");
    });

    test("it carries the opt-out through to the wrapper the same way fetchJson does", async () => {
        stub({ body: "<li>ok</li>" });
        await sendForText("/rows/", "POST", {}, { reportsItsOwnErrors: true });
        expect((calls[0]!.init as { __ulReported?: boolean }).__ulReported).toBe(true);
    });
});

describe("a body that stalls after the headers arrive", () => {
    // A real Response rejects its body read when the signal aborts.
    function stallingBody(): void {
        globalThis.fetch = ((_url: string, init: RequestInit) => {
            const body = <T>() =>
                new Promise<T>((_resolve, reject) => {
                    init.signal?.addEventListener("abort", () => reject(new Error("the body read was aborted")));
                });
            return Promise.resolve({ ok: true, status: 200, text: body<string>, json: body<unknown> } as unknown as Response);
        }) as unknown as typeof fetch;
    }

    test("fetchJson is still abandoned after the timeout", async () => {
        stallingBody();
        await expect(fetchJson("/x/", { timeoutMs: 20 })).rejects.toThrow("the body read was aborted");
    });

    test("fetchText is still abandoned after the timeout", async () => {
        stallingBody();
        await expect(fetchText("/rows/", { timeoutMs: 20 })).rejects.toThrow("the body read was aborted");
    });

    test("so is the message extraction on a refusal", async () => {
        // errorMessage() reads the body too, on a path where the caller is
        // already being told something went wrong.
        globalThis.fetch = ((_url: string, init: RequestInit) => {
            return Promise.resolve({
                ok: false,
                status: 500,
                text: () => new Promise<string>((_resolve, reject) => init.signal?.addEventListener("abort", () => reject(new Error("the body read was aborted")))),
            } as unknown as Response);
        }) as unknown as typeof fetch;
        // errorMessage swallows its own failures and falls back to the status,
        // so the abort surfaces as the generic message rather than a hang.
        await expect(fetchJson("/x/", { timeoutMs: 20 })).rejects.toThrow("HTTP 500");
    });
});
