import { beforeEach, describe, expect, mock, test } from "bun:test";

import { installLeaveConfirmation } from "./leave-confirmation";

/**
 * Each case installs its own guard, as separate pages do, and the listeners from
 * earlier cases stay bound to document for the rest of the file. So each guard
 * closes over its *own* armed flag: only the guard under test is ever armed, and
 * the leftovers stay inert instead of answering the current test's dialog.
 */
interface Guard {
    /** Arm this guard - i.e. the page now has something worth losing. */
    arm(): void;
    onConfirmed: ReturnType<typeof mock>;
}

function stubConfirm(answer: boolean): ReturnType<typeof mock> {
    const fn = mock(() => Promise.resolve(answer));
    window.confirmDialog = fn as unknown as typeof window.confirmDialog;
    return fn;
}

/** Every guard ever installed, so previous cases' guards can be disarmed. */
const installed: { armed: boolean }[] = [];

function install(overrides: Partial<Parameters<typeof installLeaveConfirmation>[0]> = {}): Guard {
    const state = { armed: false };
    installed.push(state);
    const onConfirmed = mock(() => {});
    installLeaveConfirmation({
        isBlocked: () => state.armed,
        message: "Careful.",
        onConfirmed: onConfirmed as unknown as () => void,
        ...overrides,
    });
    return {
        arm: () => {
            state.armed = true;
        },
        onConfirmed,
    };
}

function link(html = '<a id="go" href="/elsewhere/">Elsewhere</a>'): HTMLElement {
    document.body.innerHTML = html;
    return document.getElementById("go")!;
}

async function clickLink(el: HTMLElement, init: MouseEventInit = {}): Promise<MouseEvent> {
    const event = new MouseEvent("click", { bubbles: true, cancelable: true, ...init });
    el.dispatchEvent(event);
    await new Promise((resolve) => setTimeout(resolve, 5));
    return event;
}

function beforeUnload(): Event {
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    return event;
}

beforeEach(() => {
    // Leftover guards stay bound to document; disarm them so only this case's is live.
    installed.forEach((state) => {
        state.armed = false;
    });
    document.body.innerHTML = "";
    delete window.confirmDialog;
});

describe("when nothing is at stake", () => {
    test("closing the tab is not challenged", () => {
        install();
        expect(beforeUnload().defaultPrevented).toBe(false);
    });

    test("links work normally", async () => {
        install();
        expect((await clickLink(link())).defaultPrevented).toBe(false);
    });
});

describe("when the page is blocked", () => {
    test("closing the tab is challenged", () => {
        const guard = install();
        guard.arm();
        expect(beforeUnload().defaultPrevented).toBe(true);
    });

    test("a link click is intercepted and asks in the site dialog", async () => {
        const guard = install();
        guard.arm();
        const dialog = stubConfirm(false);

        expect((await clickLink(link())).defaultPrevented).toBe(true);
        expect(dialog).toHaveBeenCalled();
    });

    test("declining keeps the guard armed", async () => {
        const guard = install();
        guard.arm();
        stubConfirm(false);

        await clickLink(link());
        expect(beforeUnload().defaultPrevented).toBe(true);
        expect(guard.onConfirmed).not.toHaveBeenCalled();
    });

    test("the blocked state is re-read per event, not captured at install", async () => {
        const guard = install();
        stubConfirm(false);

        expect((await clickLink(link())).defaultPrevented).toBe(false);
        guard.arm();
        expect((await clickLink(link())).defaultPrevented).toBe(true);
    });

    test("a message function is evaluated when asked, not at install", async () => {
        let text = "first";
        const guard = install({ message: () => text });
        guard.arm();
        const dialog = stubConfirm(false);
        text = "second";

        await clickLink(link());
        expect((dialog.mock.calls[0]?.[0] as { message?: string })?.message).toBe("second");
    });

    test("falls back to the native prompt when core.js has not loaded", async () => {
        const guard = install();
        guard.arm();
        const native = mock(() => false);
        window.confirm = native as unknown as typeof window.confirm;

        await clickLink(link());
        expect(native).toHaveBeenCalled();
    });
});

describe("after agreeing to leave", () => {
    test("the browser does not ask a second time", async () => {
        // The navigation we start ourselves re-enters beforeunload. Without
        // suppression the user answers the same question twice - once in our
        // dialog, once in the browser's.
        const guard = install();
        guard.arm(); // still true: the page's own condition has not changed
        stubConfirm(true);

        await clickLink(link());
        expect(beforeUnload().defaultPrevented).toBe(false);
    });

    test("the caller's hook runs before navigating", async () => {
        const guard = install();
        guard.arm();
        stubConfirm(true);

        await clickLink(link());
        expect(guard.onConfirmed).toHaveBeenCalled();
    });

    test("a second link click is not challenged again", async () => {
        const guard = install();
        guard.arm();
        stubConfirm(true);
        await clickLink(link());

        const dialog = stubConfirm(true);
        expect((await clickLink(link())).defaultPrevented).toBe(false);
        expect(dialog).not.toHaveBeenCalled();
    });
});

describe("clicks that leave this page open", () => {
    const modifiers: [string, MouseEventInit][] = [
        ["ctrl-click", { ctrlKey: true }],
        ["cmd-click", { metaKey: true }],
        ["shift-click", { shiftKey: true }],
        ["alt-click", { altKey: true }],
        ["middle-click", { button: 1 }],
    ];

    for (const [name, init] of modifiers) {
        test(`${name} is not challenged`, async () => {
            const guard = install();
            guard.arm();
            const dialog = stubConfirm(false);

            expect((await clickLink(link(), init)).defaultPrevented).toBe(false);
            expect(dialog).not.toHaveBeenCalled();
        });
    }
});

describe("hrefs that are not navigations", () => {
    const cases: [string, string][] = [
        ["an in-page anchor", '<a id="go" href="#section">Jump</a>'],
        ["a javascript: url", '<a id="go" href="javascript:void(0)">Run</a>'],
        ["a data: url", '<a id="go" href="data:text/plain,hi">Data</a>'],
        ["a vbscript: url", '<a id="go" href="vbscript:msgbox">Legacy</a>'],
        ["an empty href", '<a id="go" href="">Empty</a>'],
        ["a new-tab link", '<a id="go" href="/elsewhere/" target="_blank">New tab</a>'],
        ["a mixed-case scheme past whitespace", '<a id="go" href="  JavaScript:void(0)">Sneaky</a>'],
        // Saves a file without navigating. It also must not be confirmed, because
        // confirming permanently disarms the guard and the page would stay behind
        // unprotected.
        ["a download link", '<a id="go" href="/export.zip" download>Download</a>'],
    ];

    for (const [name, html] of cases) {
        test(`${name} is not challenged`, async () => {
            const guard = install();
            guard.arm();
            const dialog = stubConfirm(false);

            expect((await clickLink(link(html))).defaultPrevented).toBe(false);
            expect(dialog).not.toHaveBeenCalled();
        });
    }

    test("a click that is not on a link is ignored", async () => {
        const guard = install();
        guard.arm();
        const dialog = stubConfirm(false);
        document.body.innerHTML = '<button id="go">Press</button>';

        await clickLink(document.getElementById("go")!);
        expect(dialog).not.toHaveBeenCalled();
    });

    test("a click inside a link still counts as the link", async () => {
        const guard = install();
        guard.arm();
        const dialog = stubConfirm(false);
        document.body.innerHTML = '<a href="/elsewhere/"><span id="go">Deep</span></a>';

        await clickLink(document.getElementById("go")!);
        expect(dialog).toHaveBeenCalled();
    });
});
