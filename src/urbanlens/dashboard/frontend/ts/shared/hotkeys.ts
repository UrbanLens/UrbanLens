/**
 * Site-wide customizable keyboard shortcuts.
 *
 * Every rebindable action is declared once, here, in DEFAULT_HOTKEYS. A page
 * that wants to react to one calls matchesHotkey(event, actionId) instead of
 * comparing event.key/ctrlKey/etc directly, so the user's own override (saved
 * from Settings > Shortcuts, injected as window.UL_HOTKEYS by base.html) is
 * honoured everywhere without each call site re-implementing the lookup.
 *
 * Not every existing document-level keydown handler in the app is listed
 * here - Escape/Enter in dialogs and organize panels are conventional modal
 * behavior, not a "shortcut" a user would expect to rebind, so they stay as
 * plain hardcoded key checks.
 */

export interface HotkeyDefault {
    /** Accepted combos for the default binding, e.g. ["ctrl+shift+z", "ctrl+y"] - any one matches. */
    keys: string[];
    label: string;
    description: string;
}

// No type annotation here, deliberately: leaving the literal object to infer
// its own type keeps `DEFAULT_HOTKEYS.redo` (etc.) known-present to callers
// like the contract test, rather than widening every key to `T | undefined`.
// matchesHotkey/loadHotkeys below only ever iterate it generically
// (Object.entries) or index into their own already-generic return values, so
// they don't need the wider Record<string, HotkeyDefault> shape - only a
// dynamic `DEFAULT_HOTKEYS[someString]` lookup would, and nothing does that.
export const DEFAULT_HOTKEYS = {
    undo: {
        keys: ["ctrl+z"],
        label: "Undo",
        description: "Undo the last change.",
    },
    redo: {
        keys: ["ctrl+shift+z", "ctrl+y"],
        label: "Redo",
        description: "Redo the last undone change.",
    },
    toggleFullscreen: {
        keys: ["f"],
        label: "Toggle fullscreen",
        description: "Enter or exit fullscreen while playing a game.",
    },
    openAssistant: {
        keys: ["shift+?", "?"],
        label: "Open assistant",
        description: "Open the AI assistant.",
    },
};

/** Turn a KeyboardEvent into a comparable "ctrl+shift+z"-style combo string. */
export function normalizeCombo(event: Pick<KeyboardEvent, "key" | "ctrlKey" | "metaKey" | "shiftKey" | "altKey">): string {
    const parts: string[] = [];
    if (event.ctrlKey || event.metaKey) parts.push("ctrl");
    if (event.shiftKey) parts.push("shift");
    if (event.altKey) parts.push("alt");
    parts.push(event.key.toLowerCase());
    return parts.join("+");
}

/**
 * Resolve every action's accepted combos: the user's own override (a single
 * combo, replacing the defaults entirely) where one is set, else the
 * defaults. Reads window.UL_HOTKEYS fresh each call - cheap enough (a handful
 * of actions) that caching would only add invalidation to worry about.
 */
export function loadHotkeys(): Record<string, string[]> {
    const overrides = (typeof window !== "undefined" && window.UL_HOTKEYS) || {};
    const resolved: Record<string, string[]> = {};
    for (const [actionId, def] of Object.entries(DEFAULT_HOTKEYS)) {
        const override = overrides[actionId];
        resolved[actionId] = override ? [override] : def.keys;
    }
    return resolved;
}

/** True when `event` matches the (possibly user-customized) binding for `actionId`. */
export function matchesHotkey(event: KeyboardEvent, actionId: string): boolean {
    const combos = loadHotkeys()[actionId];
    if (!combos || !combos.length) return false;
    return combos.includes(normalizeCombo(event));
}

/** Whether `target` is a form control the user could be typing into - a global hotkey listener must not fire while it's focused. */
export function isTypingTarget(target: EventTarget | null): boolean {
    if (!(target instanceof HTMLElement)) return false;
    if (target.isContentEditable) return true;
    const tag = target.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

const _MAC = typeof navigator !== "undefined" && /mac/i.test(navigator.platform || "");

/** Human-readable display for a combo string, e.g. "ctrl+shift+z" -> "Ctrl+Shift+Z" (or "⌘⇧Z" on Mac). */
export function formatHotkey(combo: string): string {
    const parts = combo.split("+");
    if (!_MAC) return parts.map((part) => (part.length === 1 ? part.toUpperCase() : part.charAt(0).toUpperCase() + part.slice(1))).join("+");
    const symbols: Record<string, string> = { ctrl: "⌘", shift: "⇧", alt: "⌥" };
    return parts.map((part) => symbols[part] ?? part.toUpperCase()).join("");
}
