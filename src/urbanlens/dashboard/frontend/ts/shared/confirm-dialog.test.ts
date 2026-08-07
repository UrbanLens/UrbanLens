/**
 * Behavioural tests for the shared confirm dialog, against a real document.
 *
 * These are the first tests in this repo that exercise DOM behaviour rather than
 * pure functions, which is what made moving this code out of ``base.html`` safe:
 * before the preload in ``testing/dom-setup.ts`` there was no way to assert that
 * clicking Cancel resolves false, only that the file typechecked.
 *
 * The lazy-binding rule is the one worth guarding. This module loads from the
 * ``<head>`` while ``#confirm-dialog`` is markup further down the body, so an
 * implementation that resolved elements at import time would capture nulls and
 * silently never open - and would still typecheck.
 */

import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";

import { confirmDialog, deletePinCascade, installGlobalConfirmDialog, resetConfirmDialogForTests } from "./confirm-dialog";

const DIALOG_MARKUP = `
  <dialog id="confirm-dialog">
    <h3 id="confirm-dialog-title"></h3>
    <button id="confirm-dialog-x"></button>
    <p id="confirm-dialog-message"></p>
    <button id="confirm-dialog-cancel"></button>
    <button id="confirm-dialog-alt" hidden></button>
    <button id="confirm-dialog-ok"></button>
  </dialog>`;

function click(id: string): void {
    document.getElementById(id)?.dispatchEvent(new Event("click"));
}

/** Wait until the dialog is showing ``title``, then click ``buttonId``.
 *
 * Counting microtasks by hand to line up with an await chain is brittle - it
 * silently becomes a timeout when the implementation gains or loses an await.
 * Waiting on the state the click depends on does not.
 */
async function clickWhenTitled(title: string, buttonId: string): Promise<void> {
    for (let attempt = 0; attempt < 200; attempt += 1) {
        if (document.getElementById("confirm-dialog-title")?.textContent === title) {
            click(buttonId);
            return;
        }
        await new Promise((resolve) => setTimeout(resolve, 1));
    }
    throw new Error(`dialog never showed "${title}"`);
}

beforeEach(() => {
    document.body.innerHTML = DIALOG_MARKUP;
    resetConfirmDialogForTests();
    localStorage.clear();
});

afterEach(() => {
    document.body.innerHTML = "";
});

describe("confirmDialog", () => {
    test("resolves true when the confirm button is clicked", async () => {
        const pending = confirmDialog({ message: "Delete it?" });
        click("confirm-dialog-ok");
        expect(await pending).toBe(true);
    });

    test("resolves false when cancelled", async () => {
        const pending = confirmDialog({ message: "Delete it?" });
        click("confirm-dialog-cancel");
        expect(await pending).toBe(false);
    });

    test("resolves 'alt' when the alternative is offered and chosen", async () => {
        const pending = confirmDialog({ message: "Children too?", altLabel: "Keep them" });
        click("confirm-dialog-alt");
        expect(await pending).toBe("alt");
    });

    test("a bare string is treated as the message", async () => {
        const pending = confirmDialog("Just a message");
        expect(document.getElementById("confirm-dialog-message")?.innerHTML).toBe("Just a message");
        click("confirm-dialog-ok");
        await pending;
    });

    test("the message is escaped, and newlines become breaks", async () => {
        const pending = confirmDialog({ message: "<img src=x>\nsecond line" });
        expect(document.getElementById("confirm-dialog-message")?.innerHTML).toBe("&lt;img src=x&gt;<br>second line");
        click("confirm-dialog-ok");
        await pending;
    });

    test("the alternative button stays hidden unless a label is given", async () => {
        const pending = confirmDialog({ message: "No alternative here" });
        expect((document.getElementById("confirm-dialog-alt") as HTMLButtonElement).hidden).toBe(true);
        click("confirm-dialog-ok");
        await pending;
    });

    test("binding is lazy, so markup added after import still works", async () => {
        // The whole reason this module resolves elements on first use: it loads from
        // the <head>, before the dialog markup exists.
        document.body.innerHTML = "";
        resetConfirmDialogForTests();
        expect(await confirmDialog({ message: "no dialog present" })).toBe(false);

        document.body.innerHTML = DIALOG_MARKUP;
        const pending = confirmDialog({ message: "now it exists" });
        click("confirm-dialog-ok");
        expect(await pending).toBe(true);
    });
});

describe("deletePinCascade", () => {
    test("a cancelled confirmation deletes nothing", async () => {
        const fetchMock = mock(() => Promise.resolve(new Response(null, { status: 204 })));
        globalThis.fetch = fetchMock as unknown as typeof fetch;

        const pending = deletePinCascade("uuid-1", "Powerhouse", "csrf");
        click("confirm-dialog-cancel");

        expect(await pending).toBe(false);
        expect(fetchMock).not.toHaveBeenCalled();
    });

    test("a successful delete flags the map's pin cache dirty", async () => {
        // The map's poll compares the newest pin's timestamp, which a deletion cannot
        // advance - without this flag the map keeps showing the deleted pin.
        globalThis.fetch = mock(() => Promise.resolve(new Response(null, { status: 204 }))) as unknown as typeof fetch;

        const pending = deletePinCascade("uuid-1", "Powerhouse", "csrf");
        click("confirm-dialog-ok");

        expect(await pending).toBe(true);
        expect(localStorage.getItem("ul_pins_dirty")).toBe("1");
    });

    test("a failed delete neither reports success nor flags the cache", async () => {
        globalThis.fetch = mock(() => Promise.resolve(new Response(null, { status: 500 }))) as unknown as typeof fetch;

        const pending = deletePinCascade("uuid-1", "Powerhouse", "csrf");
        click("confirm-dialog-ok");

        expect(await pending).toBeNull();
        expect(localStorage.getItem("ul_pins_dirty")).toBeNull();
    });

    test("a 409 asks about children and retries with the chosen mode", async () => {
        const calls: string[] = [];
        globalThis.fetch = mock((url: string) => {
            calls.push(url);
            if (calls.length === 1) {
                return Promise.resolve(new Response(JSON.stringify({ requires_children_decision: true, children: 2 }), { status: 409 }));
            }
            return Promise.resolve(new Response(null, { status: 204 }));
        }) as unknown as typeof fetch;

        const pending = deletePinCascade("uuid-1", "Powerhouse", "csrf");
        await clickWhenTitled("Delete Pin", "confirm-dialog-ok");
        await clickWhenTitled("Delete child pins too?", "confirm-dialog-ok");

        expect(await pending).toBe(true);
        expect(calls[1]).toContain("children=delete");
    });
});

describe("installGlobalConfirmDialog", () => {
    test("exposes the three globals the templates call from onclick=", () => {
        installGlobalConfirmDialog();
        expect(typeof window.confirmDialog).toBe("function");
        expect(typeof window.deletePinCascade).toBe("function");
        expect(typeof window.urbanlensConfirmExternalLink).toBe("function");
    });
});
