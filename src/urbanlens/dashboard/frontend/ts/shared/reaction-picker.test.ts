import { beforeEach, describe, expect, mock, test } from "bun:test";

import { getRecentReactionEmojis, installGlobalReactionPicker, refreshRecentReactionEmojis, saveRecentReactionEmoji, toggleReactionPicker } from "./reaction-picker";

const RECENT_KEY = "urbanlens.recentReactionEmojis";

const PICKER = (id: string, emojis: string[]) => `
  <div class="reaction-picker" id="${id}">
    <button class="reaction-add-btn">+</button>
    <div class="reaction-picker-popup" hidden>
      ${emojis.map((e) => `<button class="reaction-picker-emoji" data-emoji="${e}" hx-post="/react/${e}/">${e}</button>`).join("")}
    </div>
  </div>`;

function popup(id = "p1"): HTMLElement {
    return document.querySelector<HTMLElement>(`#${id} .reaction-picker-popup`)!;
}

function addButton(id = "p1"): HTMLElement {
    return document.querySelector<HTMLElement>(`#${id} .reaction-add-btn`)!;
}

// Installed once, as it is in production: the listeners are delegated from
// document, so re-installing per test would stack them and multiply every call.
installGlobalReactionPicker();

beforeEach(() => {
    localStorage.clear();
    document.body.innerHTML = PICKER("p1", ["👍", "🔥", "😀"]);
});

describe("the recents list", () => {
    test("is empty to begin with", () => {
        expect(getRecentReactionEmojis()).toEqual([]);
    });

    test("puts the most recent pick first", () => {
        saveRecentReactionEmoji("👍");
        saveRecentReactionEmoji("🔥");
        expect(getRecentReactionEmojis()).toEqual(["🔥", "👍"]);
    });

    test("re-picking an emoji moves it to the front without duplicating it", () => {
        saveRecentReactionEmoji("👍");
        saveRecentReactionEmoji("🔥");
        saveRecentReactionEmoji("👍");
        expect(getRecentReactionEmojis()).toEqual(["👍", "🔥"]);
    });

    test("keeps at most five", () => {
        for (const e of ["1", "2", "3", "4", "5", "6", "7"]) saveRecentReactionEmoji(e);
        expect(getRecentReactionEmojis()).toEqual(["7", "6", "5", "4", "3"]);
    });

    test("ignores an empty pick", () => {
        saveRecentReactionEmoji("");
        expect(getRecentReactionEmojis()).toEqual([]);
    });

    test("survives corrupt storage rather than throwing", () => {
        localStorage.setItem(RECENT_KEY, "not json");
        expect(getRecentReactionEmojis()).toEqual([]);
    });

    test("discards non-string entries from storage", () => {
        localStorage.setItem(RECENT_KEY, JSON.stringify(["👍", 42, null, "🔥"]));
        expect(getRecentReactionEmojis()).toEqual(["👍", "🔥"]);
    });
});

describe("the recent row", () => {
    test("clones the server's buttons so the reaction still posts", () => {
        // The clone must carry hx-post across - a hand-built button would look right
        // and silently do nothing when clicked.
        saveRecentReactionEmoji("🔥");
        refreshRecentReactionEmojis(popup());

        const recent = popup().querySelector<HTMLElement>(".reaction-picker-emoji--recent");
        expect(recent?.dataset.emoji).toBe("🔥");
        expect(recent?.getAttribute("hx-post")).toBe("/react/🔥/");
    });

    test("hands each clone to htmx, which cloneNode's attributes are inert without", () => {
        const process = mock((_el: Element) => {});
        window.htmx = { process } as unknown as typeof window.htmx;
        saveRecentReactionEmoji("🔥");
        refreshRecentReactionEmojis(popup());

        expect(process).toHaveBeenCalledTimes(1);
    });

    test("stays hidden when there is nothing recent", () => {
        refreshRecentReactionEmojis(popup());
        expect(popup().querySelector<HTMLElement>(".reaction-picker-recent")?.hidden).toBe(true);
    });

    test("skips a remembered emoji this picker does not offer", () => {
        saveRecentReactionEmoji("🦖");
        refreshRecentReactionEmojis(popup());
        expect(popup().querySelectorAll(".reaction-picker-emoji--recent")).toHaveLength(0);
    });

    test("does not clone its own clones when refreshed repeatedly", () => {
        saveRecentReactionEmoji("🔥");
        refreshRecentReactionEmojis(popup());
        refreshRecentReactionEmojis(popup());
        refreshRecentReactionEmojis(popup());
        expect(popup().querySelectorAll(".reaction-picker-emoji--recent")).toHaveLength(1);
    });

    test("reuses the one recent section rather than stacking new ones", () => {
        saveRecentReactionEmoji("🔥");
        refreshRecentReactionEmojis(popup());
        refreshRecentReactionEmojis(popup());
        expect(popup().querySelectorAll(".reaction-picker-recent")).toHaveLength(1);
    });
});

describe("opening and closing", () => {
    test("the toggle opens the popup", () => {
        toggleReactionPicker(addButton());
        expect(popup().hidden).toBe(false);
    });

    test("opening refreshes the recents, so a pick elsewhere shows up", () => {
        saveRecentReactionEmoji("👍");
        toggleReactionPicker(addButton());
        expect(popup().querySelectorAll(".reaction-picker-emoji--recent")).toHaveLength(1);
    });

    test("opening one picker closes any other", () => {
        document.body.innerHTML = PICKER("p1", ["👍"]) + PICKER("p2", ["🔥"]);
        toggleReactionPicker(addButton("p1"));
        toggleReactionPicker(addButton("p2"));

        expect(popup("p1").hidden).toBe(true);
        expect(popup("p2").hidden).toBe(false);
    });

    test("a click outside closes it", () => {
        toggleReactionPicker(addButton());
        document.body.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect(popup().hidden).toBe(true);
    });

    test("clicking an emoji records it as recent", () => {
        toggleReactionPicker(addButton());
        popup().querySelector<HTMLElement>('[data-emoji="😀"]')!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect(getRecentReactionEmojis()).toEqual(["😀"]);
    });

    test("a picker with no popup is ignored rather than throwing", () => {
        document.body.innerHTML = '<div class="reaction-picker"><button class="reaction-add-btn">+</button></div>';
        expect(() => toggleReactionPicker(document.querySelector<HTMLElement>(".reaction-add-btn")!)).not.toThrow();
    });
});

describe("installGlobalReactionPicker", () => {
    test("exposes the global the comment partials call from onclick", () => {
        expect(typeof window.toggleReactionPicker).toBe("function");
    });
});
