import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { DEFAULT_HOTKEYS, formatHotkey, loadHotkeys, matchesHotkey, normalizeCombo } from "./hotkeys";

function keyEvent(init: Partial<KeyboardEvent> & { key: string }): KeyboardEvent {
    return { ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, ...init } as KeyboardEvent;
}

describe("normalizeCombo", () => {
    test("a bare key has no modifier prefix", () => {
        expect(normalizeCombo(keyEvent({ key: "f" }))).toBe("f");
    });

    test("ctrl and metaKey both normalize to the same 'ctrl' token", () => {
        expect(normalizeCombo(keyEvent({ key: "z", ctrlKey: true }))).toBe("ctrl+z");
        expect(normalizeCombo(keyEvent({ key: "z", metaKey: true }))).toBe("ctrl+z");
    });

    test("modifiers order as ctrl, shift, alt regardless of which fired", () => {
        expect(normalizeCombo(keyEvent({ key: "z", ctrlKey: true, shiftKey: true }))).toBe("ctrl+shift+z");
        expect(normalizeCombo(keyEvent({ key: "z", altKey: true, ctrlKey: true }))).toBe("ctrl+alt+z");
    });

    test("the key itself is lowercased", () => {
        expect(normalizeCombo(keyEvent({ key: "Z", ctrlKey: true }))).toBe("ctrl+z");
    });
});

describe("matchesHotkey", () => {
    beforeEach(() => {
        (window as unknown as { UL_HOTKEYS?: Record<string, string> }).UL_HOTKEYS = undefined;
    });

    test("matches the default undo combo", () => {
        expect(matchesHotkey(keyEvent({ key: "z", ctrlKey: true }), "undo")).toBe(true);
    });

    test("redo accepts either of its two default combos", () => {
        expect(matchesHotkey(keyEvent({ key: "z", ctrlKey: true, shiftKey: true }), "redo")).toBe(true);
        expect(matchesHotkey(keyEvent({ key: "y", ctrlKey: true }), "redo")).toBe(true);
    });

    test("plain ctrl+z does not also match redo", () => {
        expect(matchesHotkey(keyEvent({ key: "z", ctrlKey: true }), "redo")).toBe(false);
    });

    test("an unknown action id never matches", () => {
        expect(matchesHotkey(keyEvent({ key: "z", ctrlKey: true }), "not-a-real-action")).toBe(false);
    });
});

describe("loadHotkeys with a profile override", () => {
    afterEach(() => {
        (window as unknown as { UL_HOTKEYS?: Record<string, string> }).UL_HOTKEYS = undefined;
    });

    test("an override replaces the defaults for that action, and only that action", () => {
        (window as unknown as { UL_HOTKEYS?: Record<string, string> }).UL_HOTKEYS = { undo: "ctrl+alt+z" };
        const resolved = loadHotkeys();
        expect(resolved.undo).toEqual(["ctrl+alt+z"]);
        expect(resolved.redo).toEqual(DEFAULT_HOTKEYS.redo.keys);
    });
});

describe("formatHotkey", () => {
    test("titlecases a plain key", () => {
        expect(formatHotkey("f")).toBe("F");
    });

    test("joins modifiers and key with '+'", () => {
        expect(formatHotkey("ctrl+shift+z")).toBe("Ctrl+Shift+Z");
    });
});
