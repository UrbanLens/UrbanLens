/**
 * The check-in chat (``partials/safety/_chat_panel.html``), shared by the owner's page and the contact portal. The same
 * socket carries the check-in's status, location and archive events, which this relays to the page as DOM events.
 */

import { CLOSE_OVER_LIMIT, CLOSE_UNAUTHORIZED, openLiveSocket, type LiveSocketHandle, type LiveSocketOptions } from "./live-socket";
import type { FetchInit } from "./site-runtime";

const NO_ACCESS = "You don't have access to this chat.";
const SEND_FAILED = "Message failed to send. You may no longer have access to this chat.";
/** Reconnect attempts before the sender is told messages may be delayed. */
const SLOW_RECONNECT_ATTEMPTS = 5;

const SHOWN_REFUSALS = new Set([400, 409, 429]);

const RELAYED: Record<string, string> = {
    status_update: "safetyStatusUpdate",
    location_update: "safetyLocationUpdate",
    archive_scheduled: "safetyArchiveScheduled",
    checkin_archived: "safetyCheckinArchived",
};

interface ChatMessage {
    sender_name: string;
    body: string;
    created: string;
}

function field(frame: object, key: string): string {
    const value: unknown = key in frame ? Reflect.get(frame, key) : undefined;
    return typeof value === "string" ? value : "";
}

function messageElement(message: ChatMessage): HTMLLIElement {
    const li = document.createElement("li");
    li.className = "safety-chat-message";
    const created = new Date(message.created);
    for (const [className, text] of [
        ["safety-chat-sender", message.sender_name],
        ["safety-chat-body", message.body],
        ["safety-chat-time", Number.isNaN(created.getTime()) ? "" : created.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })],
    ]) {
        const span = document.createElement("span");
        span.className = className ?? "";
        span.textContent = text ?? "";
        li.append(span);
    }
    return li;
}

export function installSafetyChat(panel: HTMLElement, open: (options: LiveSocketOptions) => LiveSocketHandle = openLiveSocket): LiveSocketHandle | null {
    const list = document.getElementById("safety-chat-messages");
    const empty = document.getElementById("safety-chat-empty");
    const status = document.getElementById("safety-chat-status");
    const form = document.getElementById("safety-chat-form");
    const input = document.getElementById("safety-chat-input");
    const path = panel.dataset.wsPath;
    if (!list || !(form instanceof HTMLFormElement) || !(input instanceof HTMLInputElement) || !path) return null;

    const append = (message: ChatMessage): void => {
        if (empty) empty.hidden = true;
        list.append(messageElement(message));
        list.scrollTop = list.scrollHeight;
    };
    const setConnected = (connected: boolean): void => {
        if (status) status.hidden = connected;
    };

    let closed = false;
    let failedAttempts = 0;
    setConnected(false);
    const socket = open({
        path,
        onOpen: () => {
            failedAttempts = 0;
            setConnected(true);
        },
        onClose: (code) => {
            setConnected(false);
            // A refusal is not a reconnect, and a full socket allowance already waits live-socket's longest between tries.
            if (code === CLOSE_UNAUTHORIZED || code === CLOSE_OVER_LIMIT) return;
            failedAttempts += 1;
            if (failedAttempts === SLOW_RECONNECT_ATTEMPTS) window.toastr?.warning("Still trying to reconnect to chat - messages may be delayed.");
        },
        // Revoked token or removed partner: the plain-POST fallback would only 404 too.
        onPermanentClose: () => {
            closed = true;
            if (status) {
                status.hidden = false;
                status.textContent = NO_ACCESS;
            }
            input.disabled = true;
            for (const button of form.querySelectorAll("button")) button.disabled = true;
            window.toastr?.error(NO_ACCESS);
        },
        onMessage: (frame) => {
            if (!frame || typeof frame !== "object") return;
            const type = field(frame, "type");
            if (type === "error") {
                window.toastr?.error(field(frame, "detail") || "Your message couldn't be sent.");
                return;
            }
            const event = RELAYED[type];
            if (event) {
                document.body.dispatchEvent(new CustomEvent(event, { detail: frame }));
                return;
            }
            append({ sender_name: field(frame, "sender_name"), body: field(frame, "body"), created: field(frame, "created") });
        },
    });

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (closed) return;
        const body = input.value.trim();
        if (!body) return;
        if (socket.send({ body })) {
            input.value = "";
            return;
        }
        // The socket is down: POST instead, and on failure give the message back, since a safety message that silently
        // vanished is the worst outcome here.
        const data = new FormData(form);
        data.set("body", body);
        input.value = "";
        try {
            const init: FetchInit = { method: "POST", body: data, __ulReported: true };
            const response = await fetch(form.action, init);
            if (response.ok) {
                // Off the socket, this client is not in the broadcast group and would never see its own message.
                append({ sender_name: "You", body, created: new Date().toISOString() });
                return;
            }
            // The view explains a blank or long message (400), an archived check-in (409) and a throttle (429) in plain
            // text; anything else, or a page from in front of it, is not fit to show.
            const reason = SHOWN_REFUSALS.has(response.status) ? await response.text() : "";
            throw new Error(reason && !/<[a-z!/]/i.test(reason) ? reason.trim() : SEND_FAILED);
        } catch (error) {
            input.value = body;
            window.toastr?.error(error instanceof Error && error.message ? error.message : SEND_FAILED);
        }
    });
    return socket;
}
