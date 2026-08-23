/**
 * toastr is a CDN <script>, so `window.toastr` is absent whenever that request does
 * not land. Every one of the ~130 call sites behind this helper used to throw in that
 * state, which mattered because the callers are overwhelmingly error paths - the
 * network that loses the script is the one that caused the error being reported. In
 * the floorplan editor the "could not save" toast sat directly above the call arming
 * the save retry, so a missing library turned a retried save into a lost document.
 */

import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";

import { toast } from "./dialogs";

function container(): HTMLElement | null {
    return document.getElementById("toast-container");
}

function toasts(): HTMLElement[] {
    return [...(container()?.children ?? [])] as HTMLElement[];
}

beforeEach(() => {
    document.body.innerHTML = "";
    delete (window as { toastr?: unknown }).toastr;
});

afterEach(() => {
    delete (window as { toastr?: unknown }).toastr;
});

describe("toast, with the library loaded", () => {
    test("hands the message and title straight to toastr", () => {
        const warning = mock(() => {});
        (window as { toastr?: unknown }).toastr = { success: mock(() => {}), error: mock(() => {}), warning, info: mock(() => {}) };

        toast.warning("Could not save this floorplan.", "Offline");

        expect(warning).toHaveBeenCalledTimes(1);
        expect(warning.mock.calls[0]).toEqual(["Could not save this floorplan.", "Offline"] as never);
        // The library owns the DOM in this case; we add nothing of our own.
        expect(container()).toBeNull();
    });

    test("each kind reaches the matching toastr method", () => {
        const calls: string[] = [];
        const spy = (kind: string) => () => void calls.push(kind);
        (window as { toastr?: unknown }).toastr = { success: spy("success"), error: spy("error"), warning: spy("warning"), info: spy("info") };

        toast.success("a");
        toast.error("b");
        toast.warning("c");
        toast.info("d");

        expect(calls).toEqual(["success", "error", "warning", "info"]);
    });
});

describe("toast, with the library missing", () => {
    test("does not throw - the caller's next line still runs", () => {
        // The regression this file exists for. A throw here skipped the retry that
        // the floorplan editor arms immediately after reporting a failed save.
        let reached = false;
        expect(() => {
            toast.warning("Could not save this floorplan.");
            reached = true;
        }).not.toThrow();
        expect(reached).toBe(true);
    });

    test("renders toastr's own markup, so the bundled stylesheet still applies", () => {
        // sass/_toastr.scss is ours and ships in the bundle - it styles
        // `#toast-container > .toast-{kind}` and `.toast-message`. Matching those
        // names is what makes the fallback look like every other toast.
        toast.error("Upload failed.");

        const shown = toasts();
        expect(shown).toHaveLength(1);
        expect(shown[0]?.className).toBe("toast-error");
        expect(shown[0]?.querySelector(".toast-message")?.textContent).toBe("Upload failed.");
    });

    test("the message is set as text, never as markup", () => {
        // Callers pass server-supplied strings - the save view's own error text
        // among them - straight through.
        toast.warning("<img src=x onerror=alert(1)>");

        expect(container()?.querySelector("img")).toBeNull();
        expect(toasts()[0]?.querySelector(".toast-message")?.textContent).toBe("<img src=x onerror=alert(1)>");
    });

    test("a title renders above the message, and is absent when not given", () => {
        toast.info("Save your recovery key.", "Encryption enabled");
        expect(toasts()[0]?.querySelector(".toast-title")?.textContent).toBe("Encryption enabled");

        document.body.innerHTML = "";
        toast.info("No heading here.");
        expect(toasts()[0]?.querySelector(".toast-title")).toBeNull();
    });

    test("several toasts share one container, newest first", () => {
        // toastr is configured newestOnTop in dashboard/themes/base.html.
        toast.info("first");
        toast.info("second");

        expect(document.querySelectorAll("#toast-container")).toHaveLength(1);
        expect(toasts().map((node) => node.querySelector(".toast-message")?.textContent)).toEqual(["second", "first"]);
    });

    test("clicking one dismisses it, matching tapToDismiss", () => {
        toast.info("dismiss me");
        toasts()[0]?.click();

        expect(toasts()).toHaveLength(0);
    });

    test("error and warning announce themselves assertively", () => {
        toast.error("gone wrong");
        expect(toasts()[0]?.getAttribute("role")).toBe("alert");

        document.body.innerHTML = "";
        toast.success("all good");
        expect(toasts()[0]?.getAttribute("role")).toBe("status");
    });
});
