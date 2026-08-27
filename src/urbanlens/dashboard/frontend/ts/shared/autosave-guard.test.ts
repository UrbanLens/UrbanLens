import { beforeEach, describe, expect, mock, test } from "bun:test";

import { autosaveGuard, installGlobalAutosaveGuard, resetAutosaveGuardForTests } from "./autosave-guard";

/** Stub the shared dialog, resolving with the given answer. */
function stubConfirm(answer: boolean): ReturnType<typeof mock> {
    const fn = mock(() => Promise.resolve(answer));
    window.confirmDialog = fn as unknown as typeof window.confirmDialog;
    return fn;
}

function link(html = '<a id="go" href="/elsewhere/">Elsewhere</a>'): HTMLElement {
    document.body.innerHTML = html;
    return document.getElementById("go")!;
}

/** Click and let the confirm promise settle. */
async function clickLink(el: HTMLElement, init: MouseEventInit = {}): Promise<MouseEvent> {
    const event = new MouseEvent("click", { bubbles: true, cancelable: true, ...init });
    el.dispatchEvent(event);
    await new Promise((resolve) => setTimeout(resolve, 5));
    return event;
}

// Installed once, as it is in production - the listeners are delegated.
installGlobalAutosaveGuard();

beforeEach(() => {
    resetAutosaveGuardForTests();
    document.body.innerHTML = "";
    delete window.confirmDialog;
});

describe("the blocked signal", () => {
    test("starts clear", () => {
        expect(autosaveGuard.isBlocked()).toBe(false);
    });

    test("an unsaved edit blocks", () => {
        autosaveGuard.markDirty();
        expect(autosaveGuard.isBlocked()).toBe(true);
    });

    test("saving it clears the block", () => {
        autosaveGuard.markDirty();
        autosaveGuard.markClean();
        expect(autosaveGuard.isBlocked()).toBe(false);
    });

    test("a save still in flight blocks even though the form looks clean", () => {
        // The whole point of the counter: data already sent is not yet data saved.
        autosaveGuard.saveStarted();
        expect(autosaveGuard.isBlocked()).toBe(true);
    });

    test("it clears when the request comes back", () => {
        autosaveGuard.saveStarted();
        autosaveGuard.saveFinished();
        expect(autosaveGuard.isBlocked()).toBe(false);
    });

    test("overlapping saves each have to finish", () => {
        autosaveGuard.saveStarted();
        autosaveGuard.saveStarted();
        autosaveGuard.saveFinished();
        expect(autosaveGuard.isBlocked()).toBe(true);
        autosaveGuard.saveFinished();
        expect(autosaveGuard.isBlocked()).toBe(false);
    });

    test("an unmatched finish cannot drive the count negative and disarm the guard", () => {
        autosaveGuard.saveFinished();
        autosaveGuard.saveFinished();
        autosaveGuard.saveStarted();
        expect(autosaveGuard.isBlocked()).toBe(true);
    });

    test("allowNavigation clears both signals at once", () => {
        autosaveGuard.markDirty();
        autosaveGuard.saveStarted();
        autosaveGuard.allowNavigation();
        expect(autosaveGuard.isBlocked()).toBe(false);
    });
});

describe("closing the tab", () => {
    function beforeUnload(): Event {
        const event = new Event("beforeunload", { cancelable: true });
        window.dispatchEvent(event);
        return event;
    }

    test("is not challenged when there is nothing to lose", () => {
        expect(beforeUnload().defaultPrevented).toBe(false);
    });

    test("is challenged while dirty", () => {
        autosaveGuard.markDirty();
        expect(beforeUnload().defaultPrevented).toBe(true);
    });

    test("is challenged while a save is in flight", () => {
        autosaveGuard.saveStarted();
        expect(beforeUnload().defaultPrevented).toBe(true);
    });
});

describe("clicking a link", () => {
    // The scheme/modifier/download filtering all lives in leave-confirmation.ts and
    // is covered there; these check that this guard is wired to it and that its own
    // dirty/in-flight state is what drives it.
    test("goes straight through when nothing is pending", async () => {
        const event = await clickLink(link());
        expect(event.defaultPrevented).toBe(false);
    });

    test("is intercepted while dirty", async () => {
        autosaveGuard.markDirty();
        stubConfirm(false);
        const event = await clickLink(link());
        expect(event.defaultPrevented).toBe(true);
    });

    test("is intercepted while a save is in flight", async () => {
        autosaveGuard.saveStarted();
        stubConfirm(false);
        const event = await clickLink(link());
        expect(event.defaultPrevented).toBe(true);
    });

    test("declining leaves the guard armed, so the next click asks again", async () => {
        autosaveGuard.markDirty();
        stubConfirm(false);
        await clickLink(link());
        expect(autosaveGuard.isBlocked()).toBe(true);
    });

    test("accepting clears the guard's own state", async () => {
        autosaveGuard.markDirty();
        autosaveGuard.saveStarted();
        stubConfirm(true);
        await clickLink(link());
        expect(autosaveGuard.isBlocked()).toBe(false);
    });

    test("uses the message the page set", async () => {
        autosaveGuard.markDirty();
        autosaveGuard.setMessage("Your draft is still uploading.");
        const dialog = stubConfirm(false);

        await clickLink(link());
        expect((dialog.mock.calls[0]?.[0] as { message?: string })?.message).toBe("Your draft is still uploading.");
    });
});

describe("an HTMX request", () => {
    function htmxConfirm(): { event: Event; issued: ReturnType<typeof mock> } {
        const issued = mock((_skip: boolean) => {});
        const event = new Event("htmx:confirm", { bubbles: true, cancelable: true });
        (event as Event & { detail: unknown }).detail = { issueRequest: issued };
        document.body.dispatchEvent(event);
        return { event, issued };
    }

    test("proceeds untouched when nothing is pending", () => {
        const { event } = htmxConfirm();
        expect(event.defaultPrevented).toBe(false);
    });

    test("is held back while dirty", () => {
        autosaveGuard.markDirty();
        stubConfirm(false);
        expect(htmxConfirm().event.defaultPrevented).toBe(true);
    });

    test("is re-issued once confirmed", async () => {
        autosaveGuard.markDirty();
        stubConfirm(true);

        const { issued } = htmxConfirm();
        await new Promise((resolve) => setTimeout(resolve, 5));
        expect(issued).toHaveBeenCalledWith(true);
    });

    test("is dropped when declined", async () => {
        autosaveGuard.markDirty();
        stubConfirm(false);

        const { issued } = htmxConfirm();
        await new Promise((resolve) => setTimeout(resolve, 5));
        expect(issued).not.toHaveBeenCalled();
    });
});

describe("installGlobalAutosaveGuard", () => {
    test("exposes the global the auto-saving pages report through", () => {
        expect(typeof window.autosaveGuard?.markDirty).toBe("function");
    });
});
