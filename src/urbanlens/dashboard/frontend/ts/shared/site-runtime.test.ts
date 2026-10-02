import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";

import {
    type FetchInit,
    installCsrfToken,
    installProfilePreviewGuard,
    installSubmitButtonLock,
    installValidationReports,
    responseErrorMessage,
    showServerMessages,
    SESSION_ENDED_MESSAGE,
    wrapFetch,
} from "./site-runtime";

const shown: Array<[string, string]> = [];
const realToastr = window.toastr;
const realCsrfToken = window.csrftoken;

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

afterEach(() => {
    window.toastr = realToastr;
    window.csrftoken = realCsrfToken;
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

    test("a 401 says the session ended, so the page is not left claiming a write landed (P192)", async () => {
        const report = mock((_m: string) => undefined);
        await wrapFetch(stubFetch(new Response("Your session has ended.", { status: 401 })), report)("/x/");
        expect(report).toHaveBeenCalledWith(SESSION_ENDED_MESSAGE);
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

    // A request superseded by a newer one (a keystroke, a pan, a closed dialog) rejects the same way a dead network does.
    test("a request its caller cancelled is not reported", async () => {
        const report = mock((_m: string) => undefined);
        const controller = new AbortController();
        controller.abort();
        await expect(wrapFetch(stubFetch(new DOMException("gone", "AbortError")), report)("/x/", { signal: controller.signal })).rejects.toThrow();
        expect(report).not.toHaveBeenCalled();
    });

    test("a request its caller gave up on for taking too long still reads as a timeout", async () => {
        const report = mock((_m: string) => undefined);
        const controller = new AbortController();
        const reason = new DOMException("too slow", "TimeoutError");
        controller.abort(reason);
        await expect(wrapFetch(stubFetch(reason), report)("/x/", { signal: controller.signal })).rejects.toThrow();
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

describe("installValidationReports", () => {
    installValidationReports();
    installValidationReports();

    function render(): { form: HTMLFormElement; quota: HTMLInputElement; notes: HTMLTextAreaElement; reported: string[] } {
        document.body.innerHTML = `<form><fieldset><label for="ok">OK</label><input id="ok" name="ok" value="1"><label for="quota">Quota</label><input id="quota" name="quota" required><textarea name="notes"></textarea></fieldset></form>`;
        const form = document.querySelector("form");
        const quota = document.getElementById("quota");
        const notes = document.querySelector("textarea");
        if (!form || !(quota instanceof HTMLInputElement) || !notes) throw new Error("markup");
        const reported: string[] = [];
        for (const input of document.querySelectorAll("input")) input.reportValidity = () => (reported.push(input.name), false);
        return { form, quota, notes, reported };
    }
    const halt = (form: HTMLFormElement) => form.dispatchEvent(new CustomEvent("htmx:validation:halted", { bubbles: true }));

    test("the field being typed in shows its own complaint, without moving focus", () => {
        const { form, quota, reported } = render();
        quota.focus();
        halt(form);
        expect(reported).toEqual(["quota"]);
        expect(document.activeElement).toBe(quota);
        expect(shown).toEqual([]);
    });

    test("a refused field elsewhere is named once in a toast, and focus stays where the user is typing", () => {
        const { form, quota, notes, reported } = render();
        notes.focus();
        halt(form);
        halt(form);
        expect(reported).toEqual([]);
        expect(document.activeElement).toBe(notes);
        expect(shown).toHaveLength(1);
        expect(shown[0]?.[0]).toBe("warning");
        expect(shown[0]?.[1]).toStartWith("Not saved - Quota: ");

        quota.value = "";
        form.dispatchEvent(new CustomEvent("htmx:beforeRequest", { bubbles: true }));
        halt(form);
        expect(shown).toHaveLength(2);
    });
});
