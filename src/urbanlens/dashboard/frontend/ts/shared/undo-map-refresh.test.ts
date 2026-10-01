import { beforeEach, describe, expect, test } from "bun:test";

import { installGlobalUndoMapRefresh } from "./undo-map-refresh";

/** Dispatch an htmx:afterRequest as HTMX would, bubbling from a swapped element. */
function afterRequest(detail: { successful?: boolean; requestConfig?: { verb?: string; path?: string } }): void {
    document.body.dispatchEvent(new CustomEvent("htmx:afterRequest", { bubbles: true, detail }));
}

// Installed once, as in production - the listener is delegated from body.
installGlobalUndoMapRefresh();

beforeEach(() => {
    localStorage.clear();
});

describe("after an undo restore", () => {
    test("the map's pin cache is flagged dirty", () => {
        afterRequest({ successful: true, requestConfig: { verb: "post", path: "/dashboard/undo/abc-123/restore/" } });
        expect(localStorage.getItem("ul_pins_dirty")).toBe("1");
    });

    test("an undo or redo POST also flags", () => {
        afterRequest({ successful: true, requestConfig: { verb: "post", path: "/dashboard/undo/undo/" } });
        expect(localStorage.getItem("ul_pins_dirty")).toBe("1");
        localStorage.clear();
        afterRequest({ successful: true, requestConfig: { verb: "post", path: "/dashboard/undo/redo/" } });
        expect(localStorage.getItem("ul_pins_dirty")).toBe("1");
    });

    test("a failed restore does not flag", () => {
        afterRequest({ successful: false, requestConfig: { verb: "post", path: "/dashboard/undo/abc-123/restore/" } });
        expect(localStorage.getItem("ul_pins_dirty")).toBeNull();
    });
});

describe("requests that are not restores", () => {
    const cases: [string, { verb?: string; path?: string }][] = [
        ["a GET of the history list", { verb: "get", path: "/dashboard/undo/history/" }],
        ["clearing the history", { verb: "post", path: "/dashboard/undo/clear/" }],
        ["an unrelated POST", { verb: "post", path: "/dashboard/lists/foo/delete/" }],
        ["a restore-ish path outside undo", { verb: "post", path: "/dashboard/pins/restore/" }],
    ];

    for (const [name, requestConfig] of cases) {
        test(`${name} does not flag`, () => {
            afterRequest({ successful: true, requestConfig });
            expect(localStorage.getItem("ul_pins_dirty")).toBeNull();
        });
    }

    test("a detail-less event is ignored rather than throwing", () => {
        expect(() => document.body.dispatchEvent(new CustomEvent("htmx:afterRequest", { bubbles: true }))).not.toThrow();
    });
});
