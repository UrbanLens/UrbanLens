import { beforeEach, describe, expect, test } from "bun:test";

import { installGlobalPopupDismiss } from "./popup-dismiss";

const MARKUP = `
  <div class="trip-member-rsvp-wrap">
    <button id="rsvp-btn">RSVP</button>
    <div class="rsvp-popup"><button id="rsvp-yes">Going</button></div>
  </div>
  <details class="pab-add-picker" open>
    <summary id="pab-summary">Add to album</summary>
    <button id="pab-item">Album A</button>
  </details>
  <p id="outside">elsewhere</p>`;

const rsvp = () => document.querySelector<HTMLElement>(".rsvp-popup")!;
const picker = () => document.querySelector<HTMLDetailsElement>(".pab-add-picker")!;

function click(id: string): void {
    document.getElementById(id)!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
}

// Installed once, as it is in production: the listeners are delegated from
// document, so re-installing per test would stack them and multiply every call.
installGlobalPopupDismiss();

beforeEach(() => {
    document.body.innerHTML = MARKUP;
});

describe("clicking outside", () => {
    test("closes both popups", () => {
        click("outside");
        expect(rsvp().hidden).toBe(true);
        expect(picker().open).toBe(false);
    });
});

describe("clicking inside", () => {
    test("leaves the RSVP popup open, so its own buttons stay usable", () => {
        click("rsvp-yes");
        expect(rsvp().hidden).toBe(false);
    });

    test("leaves the album picker open", () => {
        click("pab-item");
        expect(picker().open).toBe(true);
    });

    test("closes the other one - they are independent", () => {
        click("rsvp-yes");
        expect(picker().open).toBe(false);
    });
});
