import { afterEach, describe, expect, test } from "bun:test";

import { installUndoBar, registerLocalUndoProvider, resetUndoBarForTests, syncUndoBar } from "./undo-bar";

function keydown(opts: { key: string; ctrlKey?: boolean; shiftKey?: boolean; metaKey?: boolean; target?: EventTarget }): KeyboardEvent {
    return new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...opts });
}

afterEach(() => {
    resetUndoBarForTests();
    document.body.innerHTML = "";
});

describe("the floating undo bar", () => {
    test("stays hidden when neither undo nor redo is possible", () => {
        installUndoBar();
        registerLocalUndoProvider({
            canUndo: () => false,
            canRedo: () => false,
            undo: () => undefined,
            redo: () => undefined,
        });

        expect(document.getElementById("ul-undo-bar")?.hidden).toBe(true);
        expect(document.getElementById("ul-undo-btn")?.hidden).toBe(true);
        expect(document.getElementById("ul-redo-btn")?.hidden).toBe(true);
    });

    test("shows only the actions that are currently possible", () => {
        installUndoBar();
        let canUndo = true;
        let canRedo = false;
        registerLocalUndoProvider({
            canUndo: () => canUndo,
            canRedo: () => canRedo,
            undo: () => {
                canUndo = false;
                canRedo = true;
            },
            redo: () => {
                canUndo = true;
                canRedo = false;
            },
        });

        expect(document.getElementById("ul-undo-bar")?.hidden).toBe(false);
        expect(document.getElementById("ul-undo-btn")?.hidden).toBe(false);
        expect(document.getElementById("ul-redo-btn")?.hidden).toBe(true);

        document.getElementById("ul-undo-btn")?.click();
        syncUndoBar();

        expect(document.getElementById("ul-undo-btn")?.hidden).toBe(true);
        expect(document.getElementById("ul-redo-btn")?.hidden).toBe(false);
    });

    test("Ctrl+Z undoes and Ctrl+Shift+Z redoes", () => {
        installUndoBar();
        const calls: string[] = [];
        registerLocalUndoProvider({
            canUndo: () => true,
            canRedo: () => true,
            undo: () => {
                calls.push("undo");
            },
            redo: () => {
                calls.push("redo");
            },
        });

        document.dispatchEvent(keydown({ key: "z", ctrlKey: true }));
        document.dispatchEvent(keydown({ key: "z", ctrlKey: true, shiftKey: true }));
        document.dispatchEvent(keydown({ key: "y", ctrlKey: true }));

        expect(calls).toEqual(["undo", "redo", "redo"]);
    });

    test("does not steal Ctrl+Z from a text field", () => {
        installUndoBar();
        const calls: string[] = [];
        registerLocalUndoProvider({
            canUndo: () => true,
            canRedo: () => false,
            undo: () => {
                calls.push("undo");
            },
            redo: () => undefined,
        });
        const input = document.createElement("input");
        document.body.appendChild(input);

        input.dispatchEvent(keydown({ key: "z", ctrlKey: true, target: input }));

        expect(calls).toEqual([]);
    });

    test("a rapid second click while an undo is in flight sends only one request", async () => {
        // Pre-seed the bar (with its buttons) exactly as a server-rendered page
        // would, so ensureBar() finds it already present and reuses it as-is
        // (its early `if (root) return root` skips (re)building the buttons) -
        // the dataset URLs must be in place before installUndoBar() runs, since
        // its fetchStack() reads them synchronously before its first await.
        const bar = document.createElement("nav");
        bar.id = "ul-undo-bar";
        bar.hidden = true;
        bar.dataset.stackUrl = "/test/undo/stack/";
        bar.dataset.undoUrl = "/test/undo/undo/";
        bar.dataset.redoUrl = "/test/undo/redo/";
        bar.innerHTML = '<button type="button" id="ul-undo-btn" hidden></button><button type="button" id="ul-redo-btn" hidden></button>';
        document.body.appendChild(bar);

        let undoCalls = 0;
        let resolveUndoResponse: ((value: Response) => void) | undefined;
        const originalFetch = window.fetch;
        window.fetch = (async (input: RequestInfo | URL) => {
            const url = typeof input === "string" ? input : input.toString();
            if (url.includes("/undo/undo/")) {
                undoCalls++;
                return new Promise<Response>((resolve) => {
                    resolveUndoResponse = resolve;
                });
            }
            // The initial stack GET installUndoBar() fires at install time.
            return new Response(JSON.stringify({ can_undo: true, can_redo: false, undo_label: "Delete", redo_label: null }), { status: 200 });
        }) as typeof window.fetch;

        try {
            installUndoBar();
            const undoBtn = document.getElementById("ul-undo-btn") as HTMLButtonElement;
            // Let installUndoBar's fetchStack() (fetch -> response.json() ->
            // syncButtons(), each its own microtask hop) resolve so the undo
            // button is enabled before the test clicks it.
            for (let i = 0; i < 20 && undoBtn.hidden; i++) await new Promise((r) => setTimeout(r, 0));
            expect(undoBtn.hidden).toBe(false);

            undoBtn.click();
            undoBtn.click(); // rapid second click while the first request is still pending

            expect(undoCalls).toBe(1);

            resolveUndoResponse?.(new Response(JSON.stringify({ can_undo: false, can_redo: true, undo_label: null, redo_label: null }), { status: 200 }));
            await new Promise((r) => setTimeout(r, 0));
            await new Promise((r) => setTimeout(r, 0));
        } finally {
            window.fetch = originalFetch;
        }
    });

    test("lifts above another floating control in the same corner", () => {
        const collider = document.createElement("div");
        collider.className = "map-buttons";
        collider.style.cssText = "position:fixed;right:10px;bottom:10px;width:80px;height:40px;";
        document.body.appendChild(collider);
        Object.defineProperty(collider, "getBoundingClientRect", {
            value: () => ({ top: window.innerHeight - 50, bottom: window.innerHeight - 10, left: window.innerWidth - 100, right: window.innerWidth - 10, width: 90, height: 40, x: 0, y: 0, toJSON: () => ({}) }),
        });

        installUndoBar();
        registerLocalUndoProvider({
            canUndo: () => true,
            canRedo: () => false,
            undo: () => undefined,
            redo: () => undefined,
        });

        const offset = document.getElementById("ul-undo-bar")?.style.getPropertyValue("--ul-undo-offset-y");
        expect(Number.parseFloat(offset || "0")).toBeGreaterThan(40);
    });
});
