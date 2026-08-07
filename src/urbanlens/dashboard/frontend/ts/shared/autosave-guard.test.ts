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

    test("declining leaves the guard armed, so the next click asks again", async () => {
        autosaveGuard.markDirty();
        stubConfirm(false);
        await clickLink(link());
        expect(autosaveGuard.isBlocked()).toBe(true);
    });

    test("accepting disarms the guard so the navigation is not challenged twice", async () => {
        autosaveGuard.markDirty();
        stubConfirm(true);
        await clickLink(link());
        expect(autosaveGuard.isBlocked()).toBe(false);
    });

    test("falls back to the native prompt when core.js has not loaded", async () => {
        autosaveGuard.markDirty();
        const nativeConfirm = mock(() => false);
        window.confirm = nativeConfirm as unknown as typeof window.confirm;

        await clickLink(link());
        expect(nativeConfirm).toHaveBeenCalled();
    });

    test("uses the message the page set", async () => {
        autosaveGuard.markDirty();
        autosaveGuard.setMessage("Your draft is still uploading.");
        const dialog = stubConfirm(false);

        await clickLink(link());
        expect((dialog.mock.calls[0]?.[0] as { message?: string })?.message).toBe("Your draft is still uploading.");
    });
});

describe("links that do not leave the page", () => {
    const cases: [string, string][] = [
        ["an in-page anchor", '<a id="go" href="#section">Jump</a>'],
        ["a javascript: url", '<a id="go" href="javascript:void(0)">Run</a>'],
        ["a data: url", '<a id="go" href="data:text/plain,hi">Data</a>'],
        ["a vbscript: url", '<a id="go" href="vbscript:msgbox">Legacy</a>'],
        ["a new-tab link", '<a id="go" href="/elsewhere/" target="_blank">New tab</a>'],
        ["an anchor with no href value", '<a id="go" href="">Empty</a>'],
    ];

    for (const [name, html] of cases) {
        test(`${name} is not challenged`, async () => {
            autosaveGuard.markDirty();
            const dialog = stubConfirm(false);

            const event = await clickLink(link(html));
            expect(event.defaultPrevented).toBe(false);
            expect(dialog).not.toHaveBeenCalled();
        });
    }

    test("a scheme is matched case-insensitively and past leading whitespace", async () => {
        autosaveGuard.markDirty();
        const dialog = stubConfirm(false);

        await clickLink(link('<a id="go" href="  JavaScript:void(0)">Sneaky</a>'));
        expect(dialog).not.toHaveBeenCalled();
    });

    test("a click that is not on a link at all is ignored", async () => {
        autosaveGuard.markDirty();
        const dialog = stubConfirm(false);
        document.body.innerHTML = '<button id="go">Press</button>';

        await clickLink(document.getElementById("go")!);
        expect(dialog).not.toHaveBeenCalled();
    });

    test("a click on an element inside a link still counts as the link", async () => {
        autosaveGuard.markDirty();
        const dialog = stubConfirm(false);
        document.body.innerHTML = '<a href="/elsewhere/"><span id="go">Deep</span></a>';

        await clickLink(document.getElementById("go")!);
        expect(dialog).toHaveBeenCalled();
    });
});

describe("opening a link in a new tab", () => {
    // Ctrl/Cmd-click leaves this page open, so there is nothing to lose and nothing
    // to warn about. Worse, intercepting it and then navigating via location.href
    // hijacks the current tab - the opposite of what the user asked for, and it
    // discards the very changes the guard exists to protect.
    const modifiers: [string, MouseEventInit][] = [
        ["ctrl", { ctrlKey: true }],
        ["cmd", { metaKey: true }],
        ["shift", { shiftKey: true }],
        ["middle-click", { button: 1 }],
    ];

    for (const [name, init] of modifiers) {
        test(`${name} is left alone`, async () => {
            autosaveGuard.markDirty();
            const dialog = stubConfirm(false);

            const event = await clickLink(link(), init);
            expect(event.defaultPrevented).toBe(false);
            expect(dialog).not.toHaveBeenCalled();
        });
    }

    test("a plain click is still challenged", async () => {
        autosaveGuard.markDirty();
        const dialog = stubConfirm(false);

        const event = await clickLink(link());
        expect(event.defaultPrevented).toBe(true);
        expect(dialog).toHaveBeenCalled();
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
