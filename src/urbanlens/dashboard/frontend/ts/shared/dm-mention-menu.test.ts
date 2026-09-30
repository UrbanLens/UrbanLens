import { beforeEach, describe, expect, test } from "bun:test";

import { matchingMentionActions, mentionAt, MentionMenu, type MentionKind } from "./dm-mention-menu";

describe("mentionAt", () => {
    test("an @ at the start or after whitespace, with no whitespace since, is a mention", () => {
        expect(mentionAt("@pi", 3)).toEqual({ start: 0, end: 3, query: "pi" });
        expect(mentionAt("hey @", 5)).toEqual({ start: 4, end: 5, query: "" });
        expect(mentionAt("line\n@tr", 8)).toEqual({ start: 5, end: 8, query: "tr" });
    });

    test("an email address, a finished word, or a caret before the @ is not", () => {
        expect(mentionAt("me@home", 7)).toBeNull();
        expect(mentionAt("@pin now", 8)).toBeNull();
        expect(mentionAt("@x", 0)).toBeNull();
        expect(mentionAt("no at here", 10)).toBeNull();
    });
});

describe("matchingMentionActions", () => {
    test("filters by kind prefix or label substring", () => {
        expect(matchingMentionActions("").length).toBe(4);
        expect(matchingMentionActions("tr").map((a) => a.kind)).toEqual(["trip"]);
        expect(matchingMentionActions("recommend").map((a) => a.kind)).toEqual(["friend"]);
        expect(matchingMentionActions("zzz")).toEqual([]);
    });
});

describe("MentionMenu", () => {
    let picked: MentionKind[];
    let menu: MentionMenu;

    function input(): HTMLTextAreaElement {
        const el = document.getElementById("dm-composer-input");
        if (!(el instanceof HTMLTextAreaElement)) throw new Error("no composer");
        return el;
    }

    function type(value: string): void {
        input().value = value;
        input().selectionStart = input().selectionEnd = value.length;
        menu.track();
    }

    function key(name: string): boolean {
        return menu.handleKey(new KeyboardEvent("keydown", { key: name, cancelable: true }));
    }

    function items(): string[] {
        return Array.from(document.querySelectorAll<HTMLElement>("#dm-mention-menu .dm-mention-menu__item")).map((el) => `${el.dataset.kind}${el.classList.contains("is-active") ? "*" : ""}`);
    }

    beforeEach(() => {
        document.body.innerHTML = '<form id="dm-composer"><textarea id="dm-composer-input"></textarea></form>';
        picked = [];
        menu = new MentionMenu(input, (kind) => picked.push(kind));
    });

    test("typing an @ opens the menu and the query filters it", () => {
        type("hi @");
        expect(items()).toEqual(["pin*", "trip", "map", "friend"]);
        type("hi @fr");
        expect(items()).toEqual(["friend*"]);
        type("hi @fr ");
        expect(menu.isOpen).toBe(false);
        expect(document.getElementById("dm-mention-menu")).toBeNull();
    });

    test("the arrow keys move the choice, wrapping, and Enter picks it, removing the typed @query", () => {
        type("hi @");
        key("ArrowUp");
        expect(items()).toEqual(["pin", "trip", "map", "friend*"]);
        key("ArrowDown");
        key("ArrowDown");
        expect(key("Enter")).toBe(true);
        expect(picked).toEqual(["trip"]);
        expect(input().value).toBe("hi ");
        expect(menu.isOpen).toBe(false);
    });

    test("keys pass through while it is closed, and Escape closes it without picking", () => {
        type("plain");
        expect(key("Enter")).toBe(false);
        type("@");
        expect(key("Escape")).toBe(true);
        expect(menu.isOpen).toBe(false);
        expect(picked).toEqual([]);
    });

    test("the @ button inserts a spaced @ at the caret and opens on it", () => {
        input().value = "see";
        input().selectionStart = input().selectionEnd = 3;
        menu.insertTrigger();
        expect(input().value).toBe("see @");
        expect(menu.isOpen).toBe(true);
        document.querySelector<HTMLElement>('#dm-mention-menu [data-kind="map"]')?.click();
        expect(picked).toEqual(["map"]);
        expect(input().value).toBe("see ");
    });
});
