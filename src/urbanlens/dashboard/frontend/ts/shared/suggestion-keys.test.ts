import { beforeEach, expect, test } from "bun:test";

import { ACTIVE_SUGGESTION_CLASS, installSuggestionKeys } from "./suggestion-keys";

let input: HTMLInputElement;
let list: HTMLElement;
let picked: string[];

function key(name: string): KeyboardEvent {
    const event = new KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true });
    input.dispatchEvent(event);
    return event;
}

function active(): string | undefined {
    return list.querySelector<HTMLElement>(`.${ACTIVE_SUGGESTION_CLASS}`)?.textContent ?? undefined;
}

beforeEach(() => {
    document.body.innerHTML = `<input id="q"><div id="list"><button>Mill</button><button style="display:none">Hidden</button><button>Mine</button></div>`;
    input = document.getElementById("q") as HTMLInputElement;
    list = document.getElementById("list")!;
    picked = [];
    list.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => picked.push(b.textContent ?? "")));
    installSuggestionKeys({
        input,
        items: () => Array.from(list.querySelectorAll<HTMLElement>("button")).filter((b) => b.style.display !== "none"),
        isOpen: () => !list.hidden,
        close: () => {
            list.hidden = true;
        },
    });
});

test("arrow keys move the highlight through the visible suggestions and wrap", () => {
    expect(key("ArrowDown").defaultPrevented).toBe(true);
    expect(active()).toBe("Mill");
    key("ArrowDown");
    expect(active()).toBe("Mine");
    key("ArrowDown");
    expect(active()).toBe("Mill");
    key("ArrowUp");
    expect(active()).toBe("Mine");
    expect(list.querySelectorAll('[aria-selected="true"]')).toHaveLength(1);
});

test("Enter picks the highlighted suggestion", () => {
    key("ArrowDown");
    key("ArrowDown");
    expect(key("Enter").defaultPrevented).toBe(true);
    expect(picked).toEqual(["Mine"]);
});

test("Enter with nothing highlighted picks the first suggestion", () => {
    key("Enter");
    expect(picked).toEqual(["Mill"]);
});

test("Escape closes the list without letting a surrounding dialog close", () => {
    expect(key("Escape").defaultPrevented).toBe(true);
    expect(list.hidden).toBe(true);
});

test("a closed list leaves every key to the input and the form", () => {
    list.hidden = true;
    expect(key("Enter").defaultPrevented).toBe(false);
    expect(key("ArrowDown").defaultPrevented).toBe(false);
    expect(key("Escape").defaultPrevented).toBe(false);
    expect(picked).toEqual([]);
});

test("a list with nothing in it leaves Enter to the form", () => {
    list.querySelectorAll("button").forEach((b) => b.remove());
    expect(key("Enter").defaultPrevented).toBe(false);
});
