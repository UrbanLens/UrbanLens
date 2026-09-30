import { afterAll, beforeAll, expect, test } from "bun:test";

import { installVisitDialog } from "./visit-dialog";

const realHtmx = window.htmx;
const realShowModal = HTMLDialogElement.prototype.showModal;
const requests: unknown[][] = [];

beforeAll(() => {
    installVisitDialog();
    installVisitDialog();
    HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
        this.setAttribute("open", "");
    };
    window.htmx = {
        process: () => undefined,
        trigger: () => undefined,
        ajax: async (...args: unknown[]) => void requests.push(args),
    };
});

afterAll(() => {
    window.htmx = realHtmx;
    HTMLDialogElement.prototype.showModal = realShowModal;
});

test("a Log visit button loads that pin's form into the shared dialog, then opens it", async () => {
    document.body.innerHTML = `
      <button type="button" data-visit-dialog-url="/memories/visit/mill/"><i>add</i> Log visit</button>
      <dialog id="memories-visit-dialog"><div id="memories-visit-dialog-body">old form</div></dialog>`;
    document.querySelector("[data-visit-dialog-url] i")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(document.getElementById("memories-visit-dialog-body")?.textContent).toBe("Loading…");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(requests).toEqual([["GET", "/memories/visit/mill/", { target: "#memories-visit-dialog-body", swap: "innerHTML" }]]);
    expect(document.querySelector<HTMLDialogElement>("#memories-visit-dialog")?.open).toBe(true);
});
