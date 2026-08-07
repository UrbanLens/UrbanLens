/**
 * These drive the press-then-click sequencing, which is the real logic here.
 *
 * They cannot assert the geometry: happy-dom reports getBoundingClientRect as all
 * zeros, so "outside the dialog box" degenerates to "any non-zero coordinate".
 * That is enough to steer the two branches deterministically - which is what makes
 * the drag-out case testable - but a rect-comparison bug would not be caught here.
 */

import { beforeEach, describe, expect, mock, test } from "bun:test";

import { installGlobalDialogBackdrop, resetDialogBackdropForTests } from "./dialog-backdrop";

// With an all-zero rect, (0,0) reads as inside the box and anything positive as
// outside it - i.e. on the backdrop.
const INSIDE = { clientX: 0, clientY: 0 };
const BACKDROP = { clientX: 500, clientY: 500 };

function dialog(attrs = ""): HTMLDialogElement {
    document.body.innerHTML = `<dialog id="d" ${attrs}><p id="content">body</p></dialog>`;
    const el = document.getElementById("d") as HTMLDialogElement;
    el.open = true;
    // happy-dom's close() does not clear .open on a dialog opened this way.
    el.close = mock(() => {
        el.open = false;
    }) as unknown as HTMLDialogElement["close"];
    return el;
}

function press(el: Element, at: MouseEventInit): void {
    el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, ...at }));
}

function release(el: Element, at: MouseEventInit): void {
    el.dispatchEvent(new MouseEvent("click", { bubbles: true, ...at }));
}

installGlobalDialogBackdrop();

beforeEach(() => {
    resetDialogBackdropForTests();
    document.body.innerHTML = "";
});

describe("clicking the backdrop", () => {
    test("closes the dialog", () => {
        const d = dialog();
        press(d, BACKDROP);
        release(d, BACKDROP);
        expect(d.open).toBe(false);
    });

    test("calls the named teardown instead of close, when one is given", () => {
        // Dialogs holding a Leaflet map need to dispose of it on the way out.
        const teardown = mock(() => {});
        (window as unknown as Record<string, unknown>).myDialogClose = teardown;

        const d = dialog('data-closefn="myDialogClose"');
        press(d, BACKDROP);
        release(d, BACKDROP);

        expect(teardown).toHaveBeenCalled();
        expect(d.close).not.toHaveBeenCalled();
    });

    test("falls back to close when the named function is missing", () => {
        const d = dialog('data-closefn="noSuchFunction"');
        press(d, BACKDROP);
        release(d, BACKDROP);
        expect(d.open).toBe(false);
    });
});

describe("clicking inside", () => {
    test("leaves the dialog open", () => {
        const d = dialog();
        press(d, INSIDE);
        release(d, INSIDE);
        expect(d.open).toBe(true);
    });

    test("a click on the dialog's content leaves it open", () => {
        const d = dialog();
        const content = document.getElementById("content")!;
        press(content, INSIDE);
        release(content, INSIDE);
        expect(d.open).toBe(true);
    });
});

describe("dragging", () => {
    test("selecting text inside and releasing outside does not close it", () => {
        // The reason press and click are tracked separately: a plain click handler
        // would read this as a backdrop click and discard whatever was in progress.
        const d = dialog();
        press(document.getElementById("content")!, INSIDE);
        release(d, BACKDROP);
        expect(d.open).toBe(true);
    });

    test("pressing on the backdrop and releasing inside does not close it", () => {
        const d = dialog();
        press(d, BACKDROP);
        release(d, INSIDE);
        expect(d.open).toBe(true);
    });

    test("a press that started outside any dialog does not close it", () => {
        const d = dialog();
        document.body.insertAdjacentHTML("beforeend", '<button id="elsewhere">x</button>');
        press(document.getElementById("elsewhere")!, BACKDROP);
        release(d, BACKDROP);
        expect(d.open).toBe(true);
    });

    test("the backdrop press is consumed, so a stray later click does not re-close", () => {
        const d = dialog();
        press(d, BACKDROP);
        release(d, BACKDROP);
        expect(d.open).toBe(false);

        d.open = true;
        release(d, BACKDROP);
        expect(d.open).toBe(true);
    });
});

describe("a closed dialog", () => {
    test("is ignored", () => {
        const d = dialog();
        d.open = false;
        press(d, BACKDROP);
        release(d, BACKDROP);
        expect(d.close).not.toHaveBeenCalled();
    });
});
