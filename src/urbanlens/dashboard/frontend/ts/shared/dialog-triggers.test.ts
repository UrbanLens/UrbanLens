import { beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { installGlobalDialogTriggers } from "./dialog-triggers";

function dialog(id: string): HTMLDialogElement {
    return document.getElementById(id) as HTMLDialogElement;
}

describe("declarative dialog triggers", () => {
    beforeAll(() => installGlobalDialogTriggers());

    beforeEach(() => {
        document.body.innerHTML = `
          <button id="opener" type="button" data-dialog-open="d1"><i id="opener-icon">add</i></button>
          <button id="remote-closer" type="button" data-dialog-close="d1"></button>
          <dialog id="d1"><button id="closer" type="button" data-dialog-close><i id="closer-icon">x</i></button></dialog>`;
    });

    test("an opener shows the dialog it names, even when the click lands on its icon", () => {
        document.getElementById("opener-icon")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect(dialog("d1").open).toBe(true);
    });

    test("a bare closer closes the dialog it sits in", () => {
        dialog("d1").showModal();
        document.getElementById("closer-icon")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect(dialog("d1").open).toBe(false);
    });

    test("a closer with a value closes the dialog it names", () => {
        dialog("d1").showModal();
        document.getElementById("remote-closer")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect(dialog("d1").open).toBe(false);
    });

    test("an opener naming a missing dialog does nothing", () => {
        document.getElementById("opener")?.setAttribute("data-dialog-open", "nope");
        expect(() => document.getElementById("opener")?.dispatchEvent(new MouseEvent("click", { bubbles: true }))).not.toThrow();
        expect(dialog("d1").open).toBe(false);
    });
});
