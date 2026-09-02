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

    test("focuses the composer once its body actually loads, not before", async () => {
        // The real htmx.ajax swaps the fetched HTML into #assistant-overlay-body
        // and then dispatches htmx:afterSwap - the stub here does the swap by
        // hand and fires the same event, so this exercises the same listener
        // the real integration relies on.
        document.body.innerHTML = OVERLAY_MARKUP;
        window.htmx = {
            process: () => undefined,
            trigger: () => undefined,
            ajax: () => {
                const body = document.getElementById("assistant-overlay-body") as HTMLElement;
                body.innerHTML = '<input type="text" name="message">';
                body.dispatchEvent(new Event("htmx:afterSwap", { bubbles: true }));
            },
        };
        installGlobalAssistantOverlay();

        openAssistantOverlay();

        for (let attempt = 0; attempt < 200 && document.activeElement?.tagName !== "INPUT"; attempt += 1) {
            await new Promise((resolve) => setTimeout(resolve, 1));
        }
        expect(document.activeElement).toBe(document.querySelector('input[name="message"]'));
    });

    test("installing twice does not double-register listeners", () => {
        document.body.innerHTML = OVERLAY_MARKUP;
        window.htmx = { process: () => undefined, trigger: () => undefined, ajax: () => undefined };
        let keydownListeners = 0;
        const original = document.addEventListener.bind(document);
        document.addEventListener = ((type: string, listener: EventListenerOrEventListenerObject, options?: boolean | AddEventListenerOptions) => {
            if (type === "keydown") keydownListeners += 1;
            return original(type, listener, options);
        }) as typeof document.addEventListener;

        try {
            installGlobalAssistantOverlay();
            installGlobalAssistantOverlay();
        } finally {
            document.addEventListener = original;
        }

        expect(keydownListeners).toBe(1);
    });

    test("an explainer client_action redispatches ul:explainer-reopen with the id", () => {
        document.body.innerHTML = OVERLAY_MARKUP;
        window.htmx = { process: () => undefined, trigger: () => undefined, ajax: () => undefined };
        installGlobalAssistantOverlay();
        const seen: unknown[] = [];
        document.addEventListener("ul:explainer-reopen", (event) => seen.push((event as CustomEvent).detail));

        document.body.dispatchEvent(new CustomEvent("ulAssistantAction", { detail: { actions: [{ action: "reopen_explainer", id: "organize-labels-intro", kind: "explainer" }] } }));

        expect(seen).toEqual([{ id: "organize-labels-intro" }]);
    });

    test("a tour client_action redispatches ul:tour-restart with the prefix and card id", () => {
        document.body.innerHTML = OVERLAY_MARKUP;
        window.htmx = { process: () => undefined, trigger: () => undefined, ajax: () => undefined };
        installGlobalAssistantOverlay();
        const seen: unknown[] = [];
        document.addEventListener("ul:tour-restart", (event) => seen.push((event as CustomEvent).detail));

        document.body.dispatchEvent(new CustomEvent("ulAssistantAction", { detail: { actions: [{ action: "reopen_explainer", id: "drag-priority", kind: "tour", prefix: "ul_onboarding_v1_organize" }] } }));

        expect(seen).toEqual([{ prefix: "ul_onboarding_v1_organize", id: "drag-priority" }]);
    });

    test("a tour client_action missing an id is not dispatched", () => {
        document.body.innerHTML = OVERLAY_MARKUP;
        window.htmx = { process: () => undefined, trigger: () => undefined, ajax: () => undefined };
        installGlobalAssistantOverlay();
        const seen: unknown[] = [];
        document.addEventListener("ul:tour-restart", (event) => seen.push((event as CustomEvent).detail));

        document.body.dispatchEvent(new CustomEvent("ulAssistantAction", { detail: { actions: [{ action: "reopen_explainer", kind: "tour", prefix: "ul_onboarding_v1_organize" }] } }));

        expect(seen).toEqual([]);
    });

    test("an unrelated client_action is ignored", () => {
        document.body.innerHTML = OVERLAY_MARKUP;
        window.htmx = { process: () => undefined, trigger: () => undefined, ajax: () => undefined };
        installGlobalAssistantOverlay();
        const seen: unknown[] = [];
        document.addEventListener("ul:explainer-reopen", (event) => seen.push(event));
        document.addEventListener("ul:tour-restart", (event) => seen.push(event));

        expect(() => document.body.dispatchEvent(new CustomEvent("ulAssistantAction", { detail: { actions: [{ action: "some_other_action" }] } }))).not.toThrow();
        expect(seen).toEqual([]);
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
