/**
 * Behavioural tests for the shared confirm dialog, against a real document.
 */

import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";

import { confirmDialog, deletePinCascade, installGlobalConfirmDialog, resetConfirmDialogForTests } from "./confirm-dialog";
import { wrapFetch } from "./site-runtime";

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

/**
 * Wait until the dialog is showing ``title``, then click ``buttonId``.
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

const realFetch = globalThis.fetch;
const realGlobals = {
    confirmDialog: window.confirmDialog,
};

beforeEach(() => {
    document.body.innerHTML = DIALOG_MARKUP;
    resetConfirmDialogForTests();
    localStorage.clear();
});

afterEach(() => {
    document.body.innerHTML = "";
    globalThis.fetch = realFetch;
    Object.assign(window, realGlobals);
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

    test("names the cancel button as asked, and goes back to Cancel after", async () => {
        const first = confirmDialog({ message: "Cancel this check-in?", cancelLabel: "Keep it" });
        expect(document.getElementById("confirm-dialog-cancel")?.textContent).toBe("Keep it");
        click("confirm-dialog-cancel");
        await first;
        const second = confirmDialog({ message: "Delete it?" });
        expect(document.getElementById("confirm-dialog-cancel")?.textContent).toBe("Cancel");
        click("confirm-dialog-cancel");
        await second;
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

    test("a second call while one is open settles the first as cancelled instead of leaving it unresolved", async () => {
        // Without this, the second call's resolveCurrent overwrote the first's.
        const first = confirmDialog({ message: "First" });
        const second = confirmDialog({ message: "Second" });
        click("confirm-dialog-ok");

        expect(await first).toBe(false);
        expect(await second).toBe(true);
    });

    test("a question asked right after another is answered is not cancelled by the first one's late close event", async () => {
        // Chromium dispatches a dialog's close event a frame after close(), by when the 409 that asks the second
        // question ("delete its child pins too?") may already have reopened the dialog.
        const dialog = document.getElementById("confirm-dialog") as HTMLDialogElement;
        const first = confirmDialog({ message: "Delete it?" });
        click("confirm-dialog-ok");
        expect(await first).toBe(true);

        const second = confirmDialog({ message: "Its children too?" });
        dialog.dispatchEvent(new Event("close"));
        expect(dialog.open).toBe(true);
        click("confirm-dialog-ok");
        expect(await second).toBe(true);
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

    test("the 409 that asks the question raises no error toast through the site's fetch net", async () => {
        // Its callers toast their own failure on a null result, so a refused delete is still heard.
        const reports: string[] = [];
        let first = true;
        const answer = async (): Promise<Response> => {
            if (!first) return new Response(null, { status: 204 });
            first = false;
            return new Response(JSON.stringify({ requires_children_decision: true, children: 1 }), { status: 409 });
        };
        globalThis.fetch = wrapFetch(Object.assign(answer, realFetch), (m) => void reports.push(m));

        const pending = deletePinCascade("uuid-1", "Powerhouse", "csrf");
        await clickWhenTitled("Delete Pin", "confirm-dialog-ok");
        await clickWhenTitled("Delete child pins too?", "confirm-dialog-alt");

        expect(await pending).toBe(true);
        expect(reports).toEqual([]);
    });
});

describe("installGlobalConfirmDialog", () => {
    test("exposes confirmDialog", () => {
        installGlobalConfirmDialog();
        expect(typeof window.confirmDialog).toBe("function");
    });

    describe("a link marked data-confirm-external", () => {
        const realOpen = window.open;
        let opened: unknown[][] = [];

        beforeEach(() => {
            installGlobalConfirmDialog();
            opened = [];
            window.open = ((...args: unknown[]) => {
                opened.push(args);
                return null;
            }) as typeof window.open;
            document.body.insertAdjacentHTML("beforeend", `<a id="ext" href="https://example.com/x" data-confirm-external><span id="label">x</span></a><a id="plain" href="https://example.com/y">y</a>`);
        });

        afterEach(() => {
            window.open = realOpen;
        });

        function clickOn(id: string): MouseEvent {
            const event = new MouseEvent("click", { bubbles: true, cancelable: true });
            document.getElementById(id)?.dispatchEvent(event);
            return event;
        }

        test("asks first, and opens it in a new tab once confirmed", async () => {
            expect(clickOn("label").defaultPrevented).toBe(true);
            await clickWhenTitled("Leaving this site", "confirm-dialog-ok");
            await new Promise((resolve) => setTimeout(resolve, 0));
            expect(opened).toEqual([["https://example.com/x", "_blank", "noopener"]]);
        });

        test("goes nowhere when declined", async () => {
            clickOn("ext");
            await clickWhenTitled("Leaving this site", "confirm-dialog-cancel");
            await new Promise((resolve) => setTimeout(resolve, 0));
            expect(opened).toEqual([]);
        });

        test("leaves any other link alone", () => {
            expect(clickOn("plain").defaultPrevented).toBe(false);
            expect(document.getElementById("confirm-dialog-title")?.textContent).toBe("");
        });
    });
});
