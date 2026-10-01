import { beforeEach, describe, expect, test } from "bun:test";

import { replaceThreadMessages } from "./dm-thread";

function thread(bubbles: string, emptyHidden: boolean, draft = ""): string {
    return `<div class="dm-thread" id="dm-thread" data-partner-slug="bob">
      <ul class="dm-messages" id="dm-messages">${bubbles}</ul>
      <div class="dm-reply-preview" id="dm-reply-preview" hidden></div>
      <p class="dm-thread-empty" id="dm-thread-empty" ${emptyHidden ? "hidden" : ""}>No messages yet.</p>
      <div class="dm-composer-attachments" id="dm-composer-attachments"></div>
      <form class="dm-composer" id="dm-composer"><textarea id="dm-composer-input">${draft}</textarea><input type="hidden" id="dm-reply-to-id" value=""></form>
    </div>`;
}

const bubble = (id: number, text: string) => `<li class="dm-bubble" data-message-id="${id}"><span class="dm-bubble__body">${text}</span></li>`;

beforeEach(() => {
    document.body.innerHTML = `<div id="dm-thread-pane">${thread("", false)}</div>`;
});

describe("replaceThreadMessages", () => {
    test("takes the server's messages and leaves the composer as the viewer left it", () => {
        const input = document.getElementById("dm-composer-input");
        if (!(input instanceof HTMLTextAreaElement)) throw new Error("no composer");
        input.value = "half-typed draft";
        const reply = document.getElementById("dm-reply-preview");
        if (reply) reply.hidden = false;
        const replyTo = document.getElementById("dm-reply-to-id");
        if (replyTo instanceof HTMLInputElement) replyTo.value = "41";
        document.getElementById("dm-composer-attachments")?.append(Object.assign(document.createElement("span"), { className: "chip" }));

        expect(replaceThreadMessages(thread(bubble(41, "seed") + bubble(42, "meet at 40.7, -74.0"), true))).toBe(true);

        expect(Array.from(document.querySelectorAll<HTMLElement>("#dm-messages .dm-bubble"), (b) => b.dataset.messageId)).toEqual(["41", "42"]);
        expect(document.getElementById("dm-composer-input")).toBe(input);
        expect(input.value).toBe("half-typed draft");
        expect(reply?.hidden).toBe(false);
        expect(replyTo instanceof HTMLInputElement && replyTo.value).toBe("41");
        expect(document.querySelectorAll("#dm-composer-attachments .chip")).toHaveLength(1);
        expect(document.getElementById("dm-thread-empty")?.hidden).toBe(true);
    });

    test("a response without a message list changes nothing and says so", () => {
        document.getElementById("dm-messages")?.insertAdjacentHTML("beforeend", bubble(1, "kept"));
        expect(replaceThreadMessages("<p>Not found</p>")).toBe(false);
        expect(document.querySelectorAll("#dm-messages .dm-bubble")).toHaveLength(1);
    });
});
