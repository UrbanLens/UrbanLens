import { beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { buildBubble, installReactionPopovers, isGroupedWithPrevious, needsServerRender, renderReactionBar, tombstone } from "./dm-bubbles";
import { parseDmFrame, type DmMessage } from "./dm-frames";

function message(fields: Record<string, unknown>): DmMessage {
    const frame = parseDmFrame({ type: "message", id: 7, created: "2026-09-30T12:00:00Z", ...fields });
    if (frame?.type !== "message") throw new Error("not a message frame");
    return frame.message;
}

let list: HTMLUListElement;

beforeAll(() => installReactionPopovers());

beforeEach(() => {
    document.body.innerHTML = '<ul id="dm-messages"></ul>';
    const found = document.getElementById("dm-messages");
    if (!(found instanceof HTMLUListElement)) throw new Error("no list");
    list = found;
});

describe("buildBubble", () => {
    test("a peer's body and names are text, never markup", () => {
        const hostile = '<img src=x onerror="alert(1)">';
        const li = buildBubble(list, message({ body: hostile, group_uuid: "g-1", sender_name: hostile, reply_to: { sender_name: hostile, preview: hostile } }), false);
        expect(li.querySelector("img")).toBeNull();
        expect(li.querySelector(".dm-bubble__body")?.textContent).toBe(hostile);
        expect(li.querySelector(".dm-bubble__sender")?.textContent).toBe(hostile);
        expect(li.querySelector(".dm-quote-box__author")?.textContent).toBe(hostile);
    });

    test("an encrypted message carries its ciphertext for the decrypt pass, and no plaintext", () => {
        const li = buildBubble(list, message({ body: "", ciphertext: "Q1Q=", nonce: "Tk4=", key_version: 3, reply_to: { sender_name: "Ann", preview: "🔒 Message", ciphertext: "UlE=", nonce: "Uk4=", key_version: 2 } }), true);
        const body = li.querySelector<HTMLElement>(".dm-bubble__body");
        expect(body?.dataset).toMatchObject({ e2eeCt: "Q1Q=", e2eeNonce: "Tk4=", e2eeKv: "3" });
        expect(body?.classList.contains("e2ee-pending")).toBe(true);
        expect(li.querySelector<HTMLElement>(".dm-quote-box__snippet")?.dataset).toMatchObject({ e2eeCt: "UlE=", e2eeKv: "2", e2eeTruncate: "80" });
        expect(li.querySelector(".dm-lock-icon")).not.toBeNull();
    });

    test("a group message names its group for decryption and has no reaction bar", () => {
        const li = buildBubble(list, message({ ciphertext: "Q1Q=", nonce: "Tk4=", key_version: 1, group_uuid: "g-1" }), true);
        expect(li.querySelector<HTMLElement>(".dm-bubble__body")?.dataset.e2eeGroup).toBe("g-1");
        expect(li.querySelector(".dm-reactions")).toBeNull();
        expect(li.querySelector(".dm-bubble__sender")).toBeNull();
    });

    test("a photo still processing is a disabled placeholder the processing watch can find", () => {
        const li = buildBubble(list, message({ images: [{ id: 41, processing: true }, { id: 42, url: "https://x.test/p.jpg" }] }), true);
        const [pending, ready] = Array.from(li.querySelectorAll<HTMLButtonElement>(".dm-bubble__image-link"));
        expect(pending?.disabled).toBe(true);
        expect(pending?.dataset).toMatchObject({ id: "41", processing: "pending" });
        expect(pending?.querySelector("img")).toBeNull();
        expect(ready?.querySelector("img")?.getAttribute("src")).toBe("https://x.test/p.jpg");
    });
});

describe("isGroupedWithPrevious", () => {
    function seed(own: boolean, created: string): void {
        const li = document.createElement("li");
        li.className = own ? "dm-bubble dm-bubble--own" : "dm-bubble dm-bubble--them";
        li.dataset.createdAt = created;
        list.appendChild(li);
    }

    test("same side, soon after: grouped", () => {
        seed(true, "2026-09-30T12:00:00Z");
        expect(isGroupedWithPrevious(list, true, "2026-09-30T12:04:00Z")).toBe(true);
    });

    test("the other side, past the gap, or out of order: not grouped", () => {
        seed(true, "2026-09-30T12:00:00Z");
        expect(isGroupedWithPrevious(list, false, "2026-09-30T12:01:00Z")).toBe(false);
        expect(isGroupedWithPrevious(list, true, "2026-09-30T12:06:00Z")).toBe(false);
        expect(isGroupedWithPrevious(list, true, "2026-09-30T11:59:00Z")).toBe(false);
        expect(isGroupedWithPrevious(list, true, "not a date")).toBe(false);
    });
});

describe("needsServerRender", () => {
    test("maps, shares, mentions and someone else's photos go to the server; plain text and own photos do not", () => {
        expect(needsServerRender(message({ body: "hi" }), false)).toBe(false);
        expect(needsServerRender(message({ markup_map_uuid: "m" }), true)).toBe(true);
        expect(needsServerRender(message({ has_share: true }), true)).toBe(true);
        expect(needsServerRender(message({ has_location_mentions: true }), true)).toBe(true);
        expect(needsServerRender(message({ images: [{ id: 1 }] }), false)).toBe(true);
        expect(needsServerRender(message({ images: [{ id: 1 }] }), true)).toBe(false);
    });
});

describe("renderReactionBar", () => {
    test("a hostile emoji stays text and valid JSON in hx-vals", () => {
        const bar = document.createElement("div");
        const emoji = '"}<b>x</b>';
        renderReactionBar(bar, 7, "/react/7/", "me", [{ emoji, count: 2, slugs: ["me", "you"] }]);
        const pill = bar.querySelector<HTMLElement>(".dm-reaction-pill");
        expect(pill?.classList.contains("dm-reaction-pill--mine")).toBe(true);
        expect(pill?.querySelector("b")).toBeNull();
        expect(JSON.parse(pill?.getAttribute("hx-vals") ?? "")).toEqual({ emoji });
        expect(pill?.getAttribute("hx-target")).toBe("#dm-reactions-7");
    });

    test("the add button opens its own popover, closing any other, and picking closes it", () => {
        const first = document.createElement("div");
        first.className = "dm-reactions";
        const second = document.createElement("div");
        second.className = "dm-reactions";
        document.body.append(first, second);
        renderReactionBar(first, 1, "/react/1/", "me", []);
        renderReactionBar(second, 2, "/react/2/", "me", []);
        const popover = (bar: HTMLElement) => bar.querySelector<HTMLElement>(".dm-emoji-popover");
        first.querySelector<HTMLElement>(".dm-reaction-add-btn")?.click();
        expect(popover(first)?.hidden).toBe(false);
        second.querySelector<HTMLElement>(".dm-reaction-add-btn")?.click();
        expect(popover(first)?.hidden).toBe(true);
        expect(popover(second)?.hidden).toBe(false);
        popover(second)?.querySelector<HTMLElement>("button")?.click();
        expect(popover(second)?.hidden).toBe(true);
    });
});

describe("tombstone", () => {
    test("replaces the bubble's content with the marker", () => {
        const li = buildBubble(list, message({ body: "secret" }), false);
        tombstone(li, "Message deleted");
        expect(li.classList.contains("dm-bubble--tombstone")).toBe(true);
        expect(li.textContent).toBe("block Message deleted");
        expect(li.querySelector(".dm-bubble__body")).toBeNull();
    });
});

describe("parseDmFrame", () => {
    test("a field of the wrong type reads as absent", () => {
        const msg = message({ id: "7", body: 5, images: [{ id: 1 }, "junk"], reply_to: "junk", has_share: "yes" });
        expect(msg.id).toBe(0);
        expect(msg.body).toBe("");
        expect(msg.images).toEqual([{ id: 1, url: "", processingFailed: false }]);
        expect(msg.replyTo).toBeNull();
        expect(msg.hasShare).toBe(false);
    });

    test("an unhandled type, the keep-alive among them, is null", () => {
        expect(parseDmFrame({ type: "pong" })).toBeNull();
        expect(parseDmFrame(["message"])).toBeNull();
        expect(parseDmFrame(null)).toBeNull();
    });

    test("a deletion says whether it was for everyone", () => {
        expect(parseDmFrame({ type: "message_deleted", message_id: 3, scope: "everyone" })).toEqual({ type: "message_deleted", messageId: 3, everyone: true });
        expect(parseDmFrame({ type: "message_deleted", message_id: 3, scope: "self" })).toEqual({ type: "message_deleted", messageId: 3, everyone: false });
    });
});
