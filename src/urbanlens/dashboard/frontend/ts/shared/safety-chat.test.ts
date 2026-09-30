import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import type { LiveSocketHandle, LiveSocketOptions } from "./live-socket";
import { installSafetyChat } from "./safety-chat";

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

let socket: LiveSocketOptions | null = null;
let open = false;
let sent: unknown[] = [];
let toasts: string[] = [];
let posted: { url: string; body: FormData }[] = [];
let postResponse: () => Promise<Response> = async () => new Response("", { status: 200 });
const realFetch = globalThis.fetch;
const realToastr = window.toastr;

function fakeOpen(options: LiveSocketOptions): LiveSocketHandle {
    socket = options;
    return {
        send: (payload) => {
            if (!open) return false;
            sent.push(payload);
            return true;
        },
        isOpen: () => open,
        close: () => {},
    };
}

beforeEach(() => {
    socket = null;
    open = false;
    sent = [];
    toasts = [];
    posted = [];
    postResponse = async () => new Response("", { status: 200 });
    globalThis.fetch = Object.assign(
        async (input: RequestInfo | URL, init?: RequestInit) => {
            posted.push({ url: String(input), body: init?.body instanceof FormData ? init.body : new FormData() });
            return postResponse();
        },
        { preconnect: realFetch.preconnect },
    );
    window.toastr = {
        error: (message: string) => void toasts.push(`error:${message}`),
        warning: (message: string) => void toasts.push(`warning:${message}`),
        success: () => {},
        info: () => {},
        clear: () => {},
    };
    document.body.innerHTML = `
      <div id="safety-chat-panel" data-ws-path="/ws/safety/checkin/abc/chat/">
        <span id="safety-chat-status" hidden>Reconnecting...</span>
        <ul id="safety-chat-messages"></ul>
        <p id="safety-chat-empty">No messages yet.</p>
        <form id="safety-chat-form" action="/safety/abc/messages/" method="post">
          <input type="hidden" name="csrfmiddlewaretoken" value="t">
          <input type="text" name="body" id="safety-chat-input">
          <button type="submit">Send</button>
        </form>
      </div>`;
});

afterEach(() => {
    globalThis.fetch = realFetch;
    window.toastr = realToastr;
});

function install(): { panel: HTMLElement; input: HTMLInputElement; form: HTMLFormElement; status: HTMLElement; list: HTMLElement } {
    const panel = document.getElementById("safety-chat-panel");
    const input = document.getElementById("safety-chat-input");
    const form = document.getElementById("safety-chat-form");
    const status = document.getElementById("safety-chat-status");
    const list = document.getElementById("safety-chat-messages");
    if (!panel || !(input instanceof HTMLInputElement) || !(form instanceof HTMLFormElement) || !status || !list) throw new Error("fixture");
    installSafetyChat(panel, fakeOpen);
    return { panel, input, form, status, list };
}

function connect(): void {
    open = true;
    socket?.onOpen?.();
}

describe("the connection", () => {
    test("opens the panel's socket and says so while it is down", () => {
        const { status } = install();
        expect(socket?.path).toBe("/ws/safety/checkin/abc/chat/");
        expect(status.hidden).toBe(false);
        connect();
        expect(status.hidden).toBe(true);
        open = false;
        socket?.onClose?.(1006);
        expect(status.hidden).toBe(false);
    });

    test("a slow reconnect warns once, and again only after it had recovered", () => {
        install();
        for (let i = 0; i < 7; i++) socket?.onClose?.(1006);
        expect(toasts).toEqual(["warning:Still trying to reconnect to chat - messages may be delayed."]);
        connect();
        for (let i = 0; i < 5; i++) socket?.onClose?.(1006);
        expect(toasts.length).toBe(2);
    });

    test("a full allowance is not a reconnect problem", () => {
        install();
        for (let i = 0; i < 6; i++) socket?.onClose?.(4429);
        expect(toasts).toEqual([]);
    });

    test("a refusal closes the chat for good", () => {
        const { status, input, form } = install();
        socket?.onPermanentClose?.();
        expect(status.hidden).toBe(false);
        expect(status.textContent).toBe("You don't have access to this chat.");
        expect(input.disabled).toBe(true);
        expect(form.querySelector("button")?.disabled).toBe(true);
        expect(toasts).toEqual(["error:You don't have access to this chat."]);
    });
});

describe("incoming frames", () => {
    test("a message is appended as text", () => {
        const { list } = install();
        socket?.onMessage({ sender_name: "<b>Owl</b>", body: "At the gate", created: "2026-09-30T12:05:00Z" });
        expect(list.querySelectorAll("li").length).toBe(1);
        expect(list.querySelector(".safety-chat-sender")?.textContent).toBe("<b>Owl</b>");
        expect(list.querySelector(".safety-chat-body")?.textContent).toBe("At the gate");
        expect(list.querySelector(".safety-chat-time")?.textContent).not.toBe("");
        expect(document.getElementById("safety-chat-empty")?.hidden).toBe(true);
    });

    test("page events are relayed rather than shown", () => {
        install();
        const heard: string[] = [];
        for (const name of ["safetyStatusUpdate", "safetyLocationUpdate", "safetyArchiveScheduled", "safetyCheckinArchived"]) {
            document.body.addEventListener(name, (event) => heard.push(`${name}:${event instanceof CustomEvent ? event.detail.type : ""}`));
        }
        for (const type of ["status_update", "location_update", "archive_scheduled", "checkin_archived"]) socket?.onMessage({ type });
        expect(heard).toEqual(["safetyStatusUpdate:status_update", "safetyLocationUpdate:location_update", "safetyArchiveScheduled:archive_scheduled", "safetyCheckinArchived:checkin_archived"]);
        expect(document.querySelectorAll("#safety-chat-messages li").length).toBe(0);
    });

    test("an error frame is a toast", () => {
        install();
        socket?.onMessage({ type: "error", detail: "Too fast." });
        expect(toasts).toEqual(["error:Too fast."]);
    });
});

describe("sending", () => {
    test("over the socket when it is open", () => {
        const { input, form } = install();
        connect();
        input.value = "  On my way  ";
        form.requestSubmit();
        expect(sent).toEqual([{ body: "On my way" }]);
        expect(input.value).toBe("");
        expect(posted).toEqual([]);
    });

    test("by POST while it is down, showing the sender their own message", async () => {
        const { input, form, list } = install();
        input.value = "On my way";
        form.requestSubmit();
        await settle();
        expect(posted.map((p) => [p.url.endsWith("/safety/abc/messages/"), p.body.get("body"), p.body.get("csrfmiddlewaretoken")])).toEqual([[true, "On my way", "t"]]);
        expect(list.querySelector(".safety-chat-sender")?.textContent).toBe("You");
        expect(input.value).toBe("");
    });

    test("a refused POST puts the message back and shows the server's reason", async () => {
        postResponse = async () => new Response("This check-in has been archived.", { status: 409 });
        const { input, form, list } = install();
        input.value = "On my way";
        form.requestSubmit();
        await settle();
        await settle();
        expect(input.value).toBe("On my way");
        expect(toasts).toEqual(["error:This check-in has been archived."]);
        expect(list.children.length).toBe(0);
    });

    test("a throttled sender is told to slow down, not that they lost access", async () => {
        postResponse = async () => new Response("You're sending messages too quickly. Wait a moment and try again.", { status: 429 });
        const { input, form } = install();
        input.value = "On my way";
        form.requestSubmit();
        await settle();
        await settle();
        expect(toasts).toEqual(["error:You're sending messages too quickly. Wait a moment and try again."]);
    });

    test("a refusal that arrives as a page is not shown", async () => {
        postResponse = async () => new Response("<html><body><h1>429 Too Many Requests</h1></body></html>", { status: 429 });
        const { input, form } = install();
        input.value = "On my way";
        form.requestSubmit();
        await settle();
        await settle();
        expect(toasts).toEqual(["error:Message failed to send. You may no longer have access to this chat."]);
    });

    test("any other failure is generic, never the page it returned", async () => {
        postResponse = async () => new Response("<!doctype html><h1>Not Found</h1>", { status: 404 });
        const { input, form } = install();
        input.value = "On my way";
        form.requestSubmit();
        await settle();
        await settle();
        expect(input.value).toBe("On my way");
        expect(toasts).toEqual(["error:Message failed to send. You may no longer have access to this chat."]);
    });

    test("nothing is sent once the chat is closed", () => {
        const { input, form } = install();
        socket?.onPermanentClose?.();
        input.value = "hello";
        form.requestSubmit();
        expect(sent).toEqual([]);
        expect(posted).toEqual([]);
    });
});
