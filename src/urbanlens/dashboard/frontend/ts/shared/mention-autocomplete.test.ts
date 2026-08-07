import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";

import { installGlobalMentionAutocomplete, resetMentionAutocompleteForTests } from "./mention-autocomplete";

const WRAP = (attrs = "") => `
  <div class="comment-input-wrap">
    <textarea class="mention-input" ${attrs}></textarea>
    <div class="mention-dropdown" hidden></div>
  </div>`;

const realFetch = globalThis.fetch;
let requested: string[] = [];

/** Stub fetch, resolving every call with the same payload. */
function stubFetch(payload: unknown): void {
    requested = [];
    globalThis.fetch = mock((url: string | URL | Request) => {
        requested.push(String(url));
        return Promise.resolve({ json: () => Promise.resolve(payload) } as Response);
    }) as unknown as typeof fetch;
}

function textarea(): HTMLTextAreaElement {
    return document.querySelector<HTMLTextAreaElement>(".mention-input")!;
}

function dropdown(): HTMLElement {
    return document.querySelector<HTMLElement>(".mention-dropdown")!;
}

/** Type into the textarea and let the debounce and the fetch chain settle. */
async function type(value: string, caret = value.length): Promise<void> {
    const ta = textarea();
    ta.value = value;
    ta.setSelectionRange(caret, caret);
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    await new Promise((resolve) => setTimeout(resolve, 260));
}

function key(name: string): void {
    textarea().dispatchEvent(new KeyboardEvent("keydown", { key: name, bubbles: true }));
}

// Installed once, as it is in production: the listeners are delegated from
// document, so re-installing per test would stack them and multiply every call.
installGlobalMentionAutocomplete();

beforeEach(() => {
    resetMentionAutocompleteForTests();
    document.body.innerHTML = WRAP();
    stubFetch([]);
});

afterEach(() => {
    globalThis.fetch = realFetch;
});

describe("when a lookup fires", () => {
    test("a bare @ with no fragment does not query", async () => {
        await type("hello @");
        expect(requested).toHaveLength(0);
    });

    test("text with no @ does not query", async () => {
        await type("hello world");
        expect(requested).toHaveLength(0);
    });

    test("an @ followed by whitespace does not query - the caret has moved past it", async () => {
        await type("@foo bar");
        expect(requested).toHaveLength(0);
    });

    test("a fragment queries the location search", async () => {
        await type("hey @mill");
        expect(requested).toHaveLength(1);
        expect(requested[0]).toContain("/dashboard/comments/locations/?q=mill");
    });

    test("the fragment is url-encoded", async () => {
        await type("@a&b c".slice(0, 4));
        expect(requested[0]).toContain("q=a%26b");
    });

    test("only the @ nearest the caret counts", async () => {
        await type("@alpha done @bet");
        expect(requested[0]).toContain("q=bet");
    });

    test("typing quickly issues one request, not one per keystroke", async () => {
        const ta = textarea();
        for (const value of ["@m", "@mi", "@mil", "@mill"]) {
            ta.value = value;
            ta.setSelectionRange(value.length, value.length);
            ta.dispatchEvent(new Event("input", { bubbles: true }));
        }
        await new Promise((resolve) => setTimeout(resolve, 260));
        expect(requested).toHaveLength(1);
        expect(requested[0]).toContain("q=mill");
    });
});

describe("activity mentions in a trip", () => {
    beforeEach(() => {
        document.body.innerHTML = WRAP('data-context-type="trip" data-trip-slug="my trip"');
    });

    test("a numeric fragment queries the trip's map data instead", async () => {
        stubFetch({ points: [{ index: 3, label: "Old Mill" }] });
        await type("@3");
        expect(requested[0]).toContain("/dashboard/trips/my%20trip/map-data/");
    });

    test("it inserts the act: form", async () => {
        stubFetch({ points: [{ index: 3, label: "Old Mill" }] });
        await type("see @3");
        dropdown().querySelector<HTMLElement>(".mention-option")!.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
        expect(textarea().value).toBe("see @act:3 ");
    });

    test("it offers only activities whose number starts with the fragment", async () => {
        stubFetch({ points: [{ index: 1, label: "One" }, { index: 12, label: "Twelve" }, { index: 2, label: "Two" }] });
        await type("@1");
        expect(dropdown().querySelectorAll(".mention-option")).toHaveLength(2);
    });

    test("a numeric fragment outside a trip still searches locations", async () => {
        document.body.innerHTML = WRAP();
        await type("@3");
        expect(requested[0]).toContain("/dashboard/comments/locations/");
    });
});

describe("the dropdown", () => {
    test("lists the results", async () => {
        stubFetch([{ name: "Old Mill", uuid: "u1" }, { name: "New Mill", uuid: "u2" }]);
        await type("@mill");

        expect(dropdown().hidden).toBe(false);
        expect(dropdown().querySelectorAll(".mention-option")).toHaveLength(2);
    });

    test("stays hidden when nothing matches", async () => {
        await type("@zzz");
        expect(dropdown().hidden).toBe(true);
    });

    test("escapes markup in a result name", async () => {
        // Location names are user-supplied and go in via innerHTML.
        stubFetch([{ name: '<img src=x onerror=alert(1)> & co', uuid: "u1" }]);
        await type("@x");

        expect(dropdown().querySelector(".mention-option")?.innerHTML).not.toContain("<img");
        expect(dropdown().innerHTML).toContain("&amp;");
    });

    test("picking a location inserts the markup link and closes", async () => {
        stubFetch([{ name: "Old Mill", uuid: "abc-123" }]);
        await type("go to @mill");
        dropdown().querySelector<HTMLElement>(".mention-option")!.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));

        expect(textarea().value).toBe("go to @[Old Mill](loc:abc-123) ");
        expect(dropdown().hidden).toBe(true);
    });

    test("the insertion keeps text that followed the caret", async () => {
        stubFetch([{ name: "Old Mill", uuid: "u1" }]);
        const ta = textarea();
        ta.value = "go @mill tomorrow";
        ta.setSelectionRange(8, 8); // caret right after "@mill"
        ta.dispatchEvent(new Event("input", { bubbles: true }));
        await new Promise((resolve) => setTimeout(resolve, 260));
        dropdown().querySelector<HTMLElement>(".mention-option")!.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));

        // Two spaces: the insert always appends one, and the text after the caret
        // already began with one. Pre-existing cosmetic quirk, asserted here so the
        // extraction is provably faithful - see docs/PROBLEMS.md.
        expect(ta.value).toBe("go @[Old Mill](loc:u1)  tomorrow");
    });

    test("a failed request leaves the box alone rather than surfacing an error", async () => {
        globalThis.fetch = mock(() => Promise.reject(new Error("offline"))) as unknown as typeof fetch;
        await type("@mill");
        expect(dropdown().hidden).toBe(true);
    });

    test("clicking elsewhere closes it", async () => {
        stubFetch([{ name: "Old Mill", uuid: "u1" }]);
        await type("@mill");
        document.body.dispatchEvent(new MouseEvent("click", { bubbles: true }));

        expect(dropdown().hidden).toBe(true);
        expect(dropdown().innerHTML).toBe("");
    });
});

describe("keyboard navigation", () => {
    beforeEach(async () => {
        stubFetch([{ name: "A", uuid: "1" }, { name: "B", uuid: "2" }]);
        await type("@a");
    });

    const selected = () => dropdown().querySelector(".mention-option.is-selected")?.textContent;

    test("ArrowDown selects the first option, then the next", () => {
        key("ArrowDown");
        expect(selected()).toBe("A");
        key("ArrowDown");
        expect(selected()).toBe("B");
    });

    test("ArrowDown stops at the last option", () => {
        key("ArrowDown");
        key("ArrowDown");
        key("ArrowDown");
        expect(selected()).toBe("B");
    });

    test("ArrowUp stops at the first", () => {
        key("ArrowDown");
        key("ArrowUp");
        key("ArrowUp");
        expect(selected()).toBe("A");
    });

    test("only one option is ever selected", () => {
        key("ArrowDown");
        key("ArrowDown");
        expect(dropdown().querySelectorAll(".is-selected")).toHaveLength(1);
    });

    test("Enter inserts the selected option", () => {
        key("ArrowDown");
        key("Enter");
        expect(textarea().value).toBe("@[A](loc:1) ");
    });

    test("Tab does the same", () => {
        key("ArrowDown");
        key("Tab");
        expect(textarea().value).toBe("@[A](loc:1) ");
    });

    test("Enter with nothing selected leaves the text alone, so a newline still works", () => {
        key("Enter");
        expect(textarea().value).toBe("@a");
    });

    test("Escape closes the dropdown", () => {
        key("Escape");
        expect(dropdown().hidden).toBe(true);
    });
});
