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

    test("an opener's data-dialog-fill-<name> values fill the dialog's fields of that name before it opens", () => {
        document.body.innerHTML = `
          <button id="guess" type="button" data-dialog-open="d2" data-dialog-fill-latitude="41.500000" data-dialog-fill-longitude="-73.250000">Use</button>
          <input name="latitude" id="outside" value="">
          <dialog id="d2"><form><input name="latitude" id="lat"><input name="longitude" id="lng"><input name="address" id="addr" value="kept"></form></dialog>`;
        let seenOnOpen = "";
        const original = HTMLDialogElement.prototype.showModal;
        dialog("d2").showModal = function (this: HTMLDialogElement) {
            seenOnOpen = (document.getElementById("lat") as HTMLInputElement).value;
            original.call(this);
        };
        document.getElementById("guess")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));

        expect(dialog("d2").open).toBe(true);
        expect(seenOnOpen).toBe("41.500000");
        expect((document.getElementById("lng") as HTMLInputElement).value).toBe("-73.250000");
        expect((document.getElementById("addr") as HTMLInputElement).value).toBe("kept");
        expect((document.getElementById("outside") as HTMLInputElement).value).toBe("");
    });

    test("an opener naming a missing dialog does nothing", () => {
        document.getElementById("opener")?.setAttribute("data-dialog-open", "nope");
        expect(() => document.getElementById("opener")?.dispatchEvent(new MouseEvent("click", { bubbles: true }))).not.toThrow();
        expect(dialog("d1").open).toBe(false);
    });

    test("an opener with data-dialog-open-event announces the opening on <body>", () => {
        const heard: string[] = [];
        const listen = (event: Event) => void heard.push(`${event.type} ${dialog("d1").open}`);
        document.body.addEventListener("mapOverlaysOpen", listen);
        document.getElementById("opener")?.setAttribute("data-dialog-open-event", "mapOverlaysOpen");
        document.getElementById("opener-icon")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        document.body.removeEventListener("mapOverlaysOpen", listen);
        expect(heard).toEqual(["mapOverlaysOpen true"]);
    });
});

describe("a close function of the page's own", () => {
    const globals = window as unknown as Record<string, unknown>;
    let closed: string[] = [];

    beforeAll(() => installGlobalDialogTriggers());

    beforeEach(() => {
        closed = [];
        globals.closeAddPinDialog = () => {
            closed.push("closeAddPinDialog");
            dialog("add-pin").close();
        };
        globals.closeDetailPinPanel = () => void closed.push("closeDetailPinPanel");
        document.body.innerHTML = `
          <dialog id="add-pin" data-closefn="closeAddPinDialog"><button id="x" type="button" data-dialog-close>x</button></dialog>
          <dialog id="plain" data-closefn="notAFunction"><button id="plain-x" type="button" data-dialog-close>x</button></dialog>
          <div id="side-panel"><button id="panel-x" type="button" data-dialog-close data-closefn="closeDetailPinPanel">x</button></div>`;
    });

    test("a closer inside a dialog that names one runs it rather than only closing", () => {
        dialog("add-pin").showModal();
        document.getElementById("x")?.click();
        expect(closed).toEqual(["closeAddPinDialog"]);
        expect(dialog("add-pin").open).toBe(false);
    });

    test("a closer outside any dialog runs the function it names itself", () => {
        document.getElementById("panel-x")?.click();
        expect(closed).toEqual(["closeDetailPinPanel"]);
    });

    test("a name that is not a function falls back to closing the dialog", () => {
        dialog("plain").showModal();
        document.getElementById("plain-x")?.click();
        expect(dialog("plain").open).toBe(false);
    });
});
