import { afterEach, describe, expect, test } from "bun:test";

import { installGlobalAssistantOverlay, openAssistantOverlay, resetAssistantOverlayForTests } from "./assistant-overlay";

const OVERLAY_MARKUP = `
  <button type="button" id="ul-assistant-fab"></button>
  <dialog id="assistant-overlay" data-overlay-url="/assistant/overlay/">
    <button type="button" id="assistant-overlay-close"></button>
    <div id="assistant-overlay-body"></div>
  </dialog>`;

function keydown(opts: { key: string; shiftKey?: boolean; target?: EventTarget }): KeyboardEvent {
    return new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...opts });
}

afterEach(() => {
    resetAssistantOverlayForTests();
    document.body.innerHTML = "";
    delete (window as { htmx?: unknown }).htmx;
});

describe("the global assistant overlay", () => {
    test("clicking the floating button opens the dialog and loads its body once", () => {
        document.body.innerHTML = OVERLAY_MARKUP;
        const ajaxCalls: Array<[string, string]> = [];
        window.htmx = { process: () => undefined, trigger: () => undefined, ajax: (verb, url) => ajaxCalls.push([verb, url]) };
        installGlobalAssistantOverlay();

        document.getElementById("ul-assistant-fab")?.dispatchEvent(new Event("click", { bubbles: true }));

        const dialog = document.getElementById("assistant-overlay") as HTMLDialogElement;
        expect(dialog.open).toBe(true);
        expect(ajaxCalls).toEqual([["GET", "/assistant/overlay/"]]);

        dialog.close();
        document.getElementById("ul-assistant-fab")?.dispatchEvent(new Event("click", { bubbles: true }));
        // Reopening does not re-fetch - the body is loaded exactly once per page load.
        expect(ajaxCalls.length).toBe(1);
    });

    test("the close button closes the dialog", () => {
        document.body.innerHTML = OVERLAY_MARKUP;
        window.htmx = { process: () => undefined, trigger: () => undefined, ajax: () => undefined };
        installGlobalAssistantOverlay();

        openAssistantOverlay();
        const dialog = document.getElementById("assistant-overlay") as HTMLDialogElement;
        expect(dialog.open).toBe(true);

        document.getElementById("assistant-overlay-close")?.dispatchEvent(new Event("click", { bubbles: true }));
        expect(dialog.open).toBe(false);
    });

    test("the openAssistant hotkey opens the dialog", () => {
        document.body.innerHTML = OVERLAY_MARKUP;
        window.htmx = { process: () => undefined, trigger: () => undefined, ajax: () => undefined };
        installGlobalAssistantOverlay();

        document.dispatchEvent(keydown({ key: "?" }));

        expect((document.getElementById("assistant-overlay") as HTMLDialogElement).open).toBe(true);
    });

    test("the hotkey does not fire while typing in a text field", () => {
        document.body.innerHTML = OVERLAY_MARKUP;
        window.htmx = { process: () => undefined, trigger: () => undefined, ajax: () => undefined };
        installGlobalAssistantOverlay();
        const input = document.createElement("input");
        document.body.appendChild(input);

        input.dispatchEvent(keydown({ key: "?", target: input }));

        expect((document.getElementById("assistant-overlay") as HTMLDialogElement).open).toBe(false);
    });

    test("the hotkey is a no-op for a user with no overlay in the DOM", () => {
        // Ungated: base.html never renders the FAB/dialog for this profile.
        installGlobalAssistantOverlay();

        expect(() => document.dispatchEvent(keydown({ key: "?" }))).not.toThrow();
    });

    test("the floating button lifts above another floating control in the same corner", () => {
        document.body.innerHTML = OVERLAY_MARKUP;
        const collider = document.createElement("div");
        collider.id = "ul-undo-bar";
        collider.style.cssText = "position:fixed;right:10px;bottom:10px;width:80px;height:40px;";
        document.body.appendChild(collider);
        Object.defineProperty(collider, "getBoundingClientRect", {
            value: () => ({ top: window.innerHeight - 50, bottom: window.innerHeight - 10, left: window.innerWidth - 100, right: window.innerWidth - 10, width: 90, height: 40, x: 0, y: 0, toJSON: () => ({}) }),
        });

        installGlobalAssistantOverlay();

        const offset = document.getElementById("ul-assistant-fab")?.style.getPropertyValue("--ul-assistant-fab-offset-y");
        expect(Number.parseFloat(offset || "0")).toBeGreaterThan(40);
    });
});
