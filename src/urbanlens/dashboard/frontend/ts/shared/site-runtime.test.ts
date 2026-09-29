import { beforeEach, describe, expect, mock, test } from "bun:test";

import {
    type FetchInit,
    installCsrfToken,
    installProfilePreviewGuard,
    installSubmitButtonLock,
    responseErrorMessage,
    showServerMessages,
    wrapFetch,
} from "./site-runtime";

const shown: Array<[string, string]> = [];

beforeEach(() => {
    shown.length = 0;
    window.toastr = {
        success: (m: string) => void shown.push(["success", m]),
        error: (m: string) => void shown.push(["error", m]),
        warning: (m: string) => void shown.push(["warning", m]),
        info: (m: string) => void shown.push(["info", m]),
        clear: () => undefined,
    };
    document.head.innerHTML = "";
    document.body.innerHTML = "";
});

function stubFetch(result: Response | Error): (input: RequestInfo | URL, init?: RequestInit) => Promise<Response> {
    return async () => {
        if (result instanceof Error) throw result;
        return result;
    };
}

function byId<T extends HTMLElement>(id: string, type: { new (): T; prototype: T }): T {
    const el = document.getElementById(id);
    if (!(el instanceof type)) throw new Error(`#${id} missing`);
    return el;
}

describe("wrapFetch", () => {
    test("a non-2xx toasts its status", async () => {
        const report = mock((_m: string) => undefined);
        const response = await wrapFetch(stubFetch(new Response("", { status: 503 })), report)("/x/");
        expect(response.status).toBe(503);
        expect(report).toHaveBeenCalledWith("Request failed (HTTP 503).");
    });

    test("a caller that reports its own errors is left to do so, on either failure path", async () => {
        const report = mock((_m: string) => undefined);
        const init: FetchInit = { __ulReported: true };
        await wrapFetch(stubFetch(new Response("", { status: 500 })), report)("/x/", init);
        await expect(wrapFetch(stubFetch(new TypeError("offline")), report)("/x/", init)).rejects.toThrow("offline");
        expect(report).not.toHaveBeenCalled();
    });

    test("a network failure toasts and still rejects", async () => {
        const report = mock((_m: string) => undefined);
        await expect(wrapFetch(stubFetch(new TypeError("offline")), report)("/x/")).rejects.toThrow("offline");
        expect(report).toHaveBeenCalledWith("Network request failed.");
    });

    test("an abort reads as a timeout", async () => {
        const report = mock((_m: string) => undefined);
        await expect(wrapFetch(stubFetch(new DOMException("gone", "AbortError")), report)("/x/")).rejects.toThrow();
        expect(report).toHaveBeenCalledWith("Request timed out.");
    });

    test("a 2xx is silent, and the wrapper marks itself so it is not wrapped twice", async () => {
        const report = mock((_m: string) => undefined);
        const wrapped = wrapFetch(stubFetch(new Response("{}")), report);
        await wrapped("/x/");
        expect(report).not.toHaveBeenCalled();
        expect(wrapped.__urbanLensWrapped).toBe(true);
    });
});

describe("responseErrorMessage", () => {
    test("a short plain-text body is shown as sent", () => {
        expect(responseErrorMessage(400, "  That name is taken.  ")).toBe("That name is taken.");
    });

    test("an HTML error page falls back to the status", () => {
        expect(responseErrorMessage(500, "<!doctype html><h1>Server Error</h1>")).toBe("Request failed (HTTP 500).");
        expect(responseErrorMessage(0, "")).toBe("Request failed.");
    });
});

describe("server messages", () => {
    test("each message toasts at its level, and an unknown tag as info, as text", () => {
        document.body.innerHTML =
            '<template id="ul-messages"><span data-level="success">Saved &lt;b&gt;</span><span data-level="error extra">Odd</span></template>';
        showServerMessages();
        expect(shown).toEqual([
            ["success", "Saved <b>"],
            ["info", "Odd"],
        ]);
    });
});

describe("installCsrfToken", () => {
    test("the meta token becomes window.csrftoken and rides on every htmx request", () => {
        document.head.innerHTML = '<meta name="csrf-token" content="tok-1">';
        installCsrfToken();
        expect(window.csrftoken).toBe("tok-1");
        const headers: Record<string, string> = {};
        document.body.dispatchEvent(new CustomEvent("htmx:configRequest", { bubbles: true, detail: { headers } }));
        expect(headers["X-CSRFToken"]).toBe("tok-1");
    });
});

describe("installSubmitButtonLock", () => {
    test("a form's submit button is disabled while its request runs", () => {
        installSubmitButtonLock();
        document.body.innerHTML = '<form id="f"><button class="btn--submit" id="b">Save</button></form>';
        const form = byId("f", HTMLFormElement);
        const btn = byId("b", HTMLButtonElement);
        form.dispatchEvent(new CustomEvent("htmx:beforeRequest", { bubbles: true }));
        expect(btn.disabled).toBe(true);
        form.dispatchEvent(new CustomEvent("htmx:afterRequest", { bubbles: true }));
        expect(btn.disabled).toBe(false);
    });
});

describe("installProfilePreviewGuard", () => {
    test("while previewing, a control is inert but the banner's exit still works", () => {
        installProfilePreviewGuard();
        document.body.innerHTML = '<div class="profile-preview-banner"><a id="exit" href="#">Exit</a></div><button id="act">Follow</button>';
        const act = mock(() => undefined);
        const exit = mock(() => undefined);
        document.getElementById("act")?.addEventListener("click", act);
        document.getElementById("exit")?.addEventListener("click", (e) => {
            e.preventDefault();
            exit();
        });
        document.getElementById("act")?.click();
        document.getElementById("exit")?.click();
        expect(act).not.toHaveBeenCalled();
        expect(exit).toHaveBeenCalled();
        expect(shown.map(([level]) => level)).toEqual(["warning"]);
    });
});
