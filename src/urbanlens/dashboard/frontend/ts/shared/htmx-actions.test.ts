import { beforeEach, describe, expect, test } from "bun:test";

import { installGlobalHtmxActions, registerHtmxAction, resetHtmxActions, type HtmxRequestDetail } from "./htmx-actions";

installGlobalHtmxActions();

function fire(target: Element, name: string, detail: HtmxRequestDetail = {}): CustomEvent {
    const event = new CustomEvent(name, { bubbles: true, cancelable: true, detail: { elt: target, ...detail } });
    target.dispatchEvent(event);
    return event;
}

/** One request's lifecycle as htmx dispatches it on the requesting element. */
function request(target: Element, detail: HtmxRequestDetail = {}): void {
    fire(target, "htmx:beforeRequest", detail);
    fire(target, "htmx:afterRequest", detail);
}

function xhr(status: number, body = "", headers: Record<string, string> = {}): XMLHttpRequest {
    return { status, responseText: body, getResponseHeader: (name: string) => headers[name] ?? null } as unknown as XMLHttpRequest;
}

beforeEach(() => {
    resetHtmxActions();
    document.body.innerHTML = "";
});

describe("data-ul-on-success", () => {
    test("runs its actions only when the request succeeded", () => {
        document.body.innerHTML = '<dialog id="d" open><form data-ul-on-success="close-dialog"><button></button></form></dialog>';
        const form = document.querySelector("form")!;
        const dialog = document.getElementById("d") as HTMLDialogElement;

        request(form, { successful: false });
        expect(dialog.open).toBe(true);

        request(form, { successful: true });
        expect(dialog.open).toBe(false);
    });

    test("sees a descendant's request, with itself as the action's element", () => {
        document.body.innerHTML = '<dialog id="d" open data-ul-on-success="close-dialog"><button hx-post="/x"></button></dialog>';

        request(document.querySelector("button")!, { successful: true });

        expect((document.getElementById("d") as HTMLDialogElement).open).toBe(false);
    });

    test("still runs when the swap detached the element before htmx:afterRequest", () => {
        document.body.innerHTML = '<div id="panel"><form data-ul-on-success="dispatch:done"></form></div>';
        const form = document.querySelector("form")!;
        let seen = 0;
        document.body.addEventListener("done", () => seen++);

        fire(form, "htmx:beforeRequest");
        document.getElementById("panel")!.innerHTML = "";
        fire(form, "htmx:afterRequest", { successful: true });

        expect(seen).toBe(1);
    });

    test("runs tokens in order with their arguments", () => {
        document.body.innerHTML = '<form data-ul-on-success="first second:two"></form>';
        const calls: string[] = [];
        registerHtmxAction("first", (_el, _e, arg) => calls.push(`first(${arg})`));
        registerHtmxAction("second", (_el, _e, arg) => calls.push(`second(${arg})`));

        request(document.querySelector("form")!, { successful: true });

        expect(calls).toEqual(["first()", "second(two)"]);
    });

    test("data-ul-success-status narrows success to one status", () => {
        document.body.innerHTML = '<form data-ul-on-success="hit" data-ul-success-status="200"></form>';
        let hits = 0;
        registerHtmxAction("hit", () => hits++);
        const form = document.querySelector("form")!;

        request(form, { successful: true, xhr: xhr(204) });
        expect(hits).toBe(0);
        request(form, { successful: true, xhr: xhr(200) });
        expect(hits).toBe(1);
    });

    test("data-ul-success-verb narrows success to one verb", () => {
        document.body.innerHTML = '<dialog open data-ul-on-success="close-dialog" data-ul-success-verb="post"><div></div></dialog>';
        const dialog = document.querySelector("dialog")!;

        request(dialog.querySelector("div")!, { successful: true, requestConfig: { verb: "get" } });
        expect(dialog.open).toBe(true);
        request(dialog.querySelector("div")!, { successful: true, requestConfig: { verb: "post" } });
        expect(dialog.open).toBe(false);
    });

    test("data-ul-success-toast shows its message", () => {
        document.body.innerHTML = '<form data-ul-success-toast="Saved &amp; done."></form>';

        request(document.querySelector("form")!, { successful: true });

        expect(document.querySelector(".toast-success .toast-message")?.textContent).toBe("Saved & done.");
    });

    test("an unknown action is skipped, not thrown", () => {
        document.body.innerHTML = '<form data-ul-on-success="nope reset"><input name="q" value="typed"></form>';
        const input = document.querySelector("input")!;
        input.value = "changed";

        expect(() => request(document.querySelector("form")!, { successful: true })).not.toThrow();
        expect(input.value).toBe("typed");
    });
});

describe("builtin actions", () => {
    test("show-modal, close, hide and remove act on the element with that id", () => {
        document.body.innerHTML =
            '<dialog id="a"></dialog><dialog id="b" open></dialog><p id="c"></p><p id="e"></p>' +
            '<form data-ul-on-success="show-modal:a close:b hide:c remove:e"></form>';

        request(document.querySelector("form")!, { successful: true });

        expect((document.getElementById("a") as HTMLDialogElement).open).toBe(true);
        expect((document.getElementById("b") as HTMLDialogElement).open).toBe(false);
        expect(document.getElementById("c")!.hidden).toBe(true);
        expect(document.getElementById("e")).toBeNull();
    });

    test("clear-field empties the named field only", () => {
        document.body.innerHTML = '<form data-ul-on-success="clear-field:email_input"><input name="email_input"><input name="other"></form>';
        const [email, other] = Array.from(document.querySelectorAll("input"));
        email!.value = "a@b.c";
        other!.value = "keep";

        request(document.querySelector("form")!, { successful: true });

        expect(email!.value).toBe("");
        expect(other!.value).toBe("keep");
    });

    test("data-ul-after-request runs whatever the outcome", () => {
        document.body.innerHTML = '<form data-ul-after-request="dispatch:refreshPriority"></form>';
        let seen = 0;
        document.body.addEventListener("refreshPriority", () => seen++);

        request(document.querySelector("form")!, { successful: false });

        expect(seen).toBe(1);
    });

    test("data-ul-before-request marks the row as deleting before the response", () => {
        document.body.innerHTML = '<ul><li><form data-ul-before-request="mark-deleting"></form></li></ul>';

        fire(document.querySelector("form")!, "htmx:beforeRequest");

        expect(document.querySelector("li")!.classList.contains("is-deleting")).toBe(true);
    });

    test("redirect-json leaves the page for a same-origin path only", () => {
        document.body.innerHTML = '<form data-ul-on-success="redirect-json"></form>';
        const before = window.location.href;

        request(document.querySelector("form")!, { successful: true, xhr: xhr(200, JSON.stringify({ redirect: "https://evil.test/" })) });
        request(document.querySelector("form")!, { successful: true, xhr: xhr(200, JSON.stringify({ redirect: "//evil.test/" })) });
        request(document.querySelector("form")!, { successful: true, xhr: xhr(200, "not json") });

        expect(window.location.href).toBe(before);
    });
});

describe("data-ul-min-query", () => {
    function confirm(value: string): boolean {
        document.body.innerHTML = '<input data-ul-min-query="2">';
        const input = document.querySelector("input")!;
        input.value = value;
        return fire(input, "htmx:confirm").defaultPrevented;
    }

    test("holds back a query shorter than the minimum", () => {
        expect(confirm("a")).toBe(true);
    });

    test("lets an empty query through, so clearing resets the results", () => {
        expect(confirm("")).toBe(false);
    });

    test("lets a query at the minimum through", () => {
        expect(confirm("ab")).toBe(false);
    });
});
