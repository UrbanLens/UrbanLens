/**
 * The shared confirm dialog, and the two flows built on it.
 */

import { escHtml } from "./escape-html";
import type { FetchInit } from "./site-runtime";

interface ConfirmOptions {
    title?: string;
    message?: string;
    confirmLabel?: string;
    cancelLabel?: string;
    /** Shows a third button; picking it resolves with ``"alt"``. */
    altLabel?: string;
    /** ``false`` renders the primary button as non-destructive. */
    danger?: boolean;
}

/** ``true`` confirmed, ``false`` cancelled or dismissed, ``"alt"`` chose the alternative. */
export type ConfirmResult = boolean | "alt";

interface DialogParts {
    dialog: HTMLDialogElement;
    ok: HTMLElement;
    cancel: HTMLElement | null;
    alt: HTMLElement;
    title: HTMLElement;
    message: HTMLElement;
}

let parts: DialogParts | null = null;
let resolveCurrent: ((result: ConfirmResult) => void) | null = null;

function settle(result: ConfirmResult): void {
    if (resolveCurrent) {
        const resolve = resolveCurrent;
        resolveCurrent = null;
        resolve(result);
    }
    if (parts?.dialog.open) parts.dialog.close();
}

/** Resolve the dialog's elements and wire them, once, on first use. */
function dialogParts(): DialogParts | null {
    if (parts) return parts;

    const dialog = document.getElementById("confirm-dialog") as HTMLDialogElement | null;
    const ok = document.getElementById("confirm-dialog-ok");
    const alt = document.getElementById("confirm-dialog-alt");
    const title = document.getElementById("confirm-dialog-title");
    const message = document.getElementById("confirm-dialog-message");
    if (!dialog || !ok || !alt || !title || !message) return null;

    const cancel = document.getElementById("confirm-dialog-cancel");
    parts = { dialog, ok, cancel, alt, title, message };
    cancel?.addEventListener("click", () => settle(false));
    document.getElementById("confirm-dialog-x")?.addEventListener("click", () => settle(false));
    ok.addEventListener("click", () => settle(true));
    alt.addEventListener("click", () => settle("alt"));
    // Catches every close path at once: backdrop click, Escape, and direct .close(). The event arrives a frame late,
    // so one reaching a dialog already reopened for the next question belongs to the last one.
    dialog.addEventListener("close", () => {
        if (!dialog.open) settle(false);
    });
    return parts;
}

/** Reset the cached elements. Test-only: a fresh document invalidates them. */
export function resetConfirmDialogForTests(): void {
    parts = null;
    resolveCurrent = null;
}

export function confirmDialog(options: ConfirmOptions | string): Promise<ConfirmResult> {
    const opts: ConfirmOptions = typeof options === "string" ? { message: options } : (options ?? {});
    const found = dialogParts();
    // No dialog markup on this page - refuse rather than throwing into an onclick,
    // which would leave the click looking like it did nothing.
    if (!found) return Promise.resolve(false);

    // The dialog is a page-wide singleton.
    if (found.dialog.open) settle(false);

    found.title.textContent = opts.title || "Are you sure?";
    found.message.innerHTML = escHtml(opts.message).replace(/\n/g, "<br>");
    found.ok.textContent = opts.confirmLabel || "Confirm";
    if (found.cancel) found.cancel.textContent = opts.cancelLabel || "Cancel";
    found.ok.className = opts.danger === false ? "btn btn--primary" : "btn--danger-filled";
    (found.alt as HTMLElement & { hidden: boolean }).hidden = !opts.altLabel;
    if (opts.altLabel) found.alt.textContent = opts.altLabel;

    return new Promise<ConfirmResult>((resolve) => {
        resolveCurrent = resolve;
        found.dialog.showModal();
    });
}

/** A click on an ``a[data-confirm-external]`` (a community-added link) confirms before opening it. */
function onExternalLinkClick(event: MouseEvent): void {
    const link = event.target instanceof Element ? event.target.closest<HTMLAnchorElement>("a[data-confirm-external]") : null;
    if (!link) return;
    event.preventDefault();
    const url = link.href;
    void confirmDialog({
        title: "Leaving this site",
        message: `This link was added by the community and leads to an external, untrusted site:\n${url}`,
        confirmLabel: "Continue",
        danger: false,
    }).then((ok) => {
        if (ok) window.open(url, "_blank", "noopener");
    });
}

/**
 * Delete a pin, letting the server veto with a 409 when it has children so the user decides whether those go too.
 */
export async function deletePinCascade(pinUuid: string, pinName: string, csrfToken: string): Promise<boolean | null> {
    const confirmed = await confirmDialog({
        title: "Delete Pin",
        message: `Delete "${pinName || "this pin"}"?\n\nThe pin and its photos can be restored from Settings → Undo History. Comments, albums and links are deleted permanently.`,
        confirmLabel: "Delete",
    });
    if (!confirmed) return false;

    const url = `/dashboard/rest/pins/${encodeURIComponent(pinUuid)}/`;
    // A 409 is the question below, not a failure; every caller toasts a null result itself.
    const init: FetchInit = { method: "DELETE", headers: { "X-CSRFToken": csrfToken }, __ulReported: true };
    const send = (query: string): Promise<Response> => fetch(url + query, init);

    let response: Response;
    try {
        response = await send("");
    } catch {
        return null;
    }

    if (response.status === 409) {
        let data: { requires_children_decision?: boolean; children?: number } | null = null;
        try {
            data = await response.json();
        } catch {

        }
        if (!data?.requires_children_decision) return null;

        const n = data.children ?? 0;
        const plural = n === 1 ? "" : "s";
        const them = n === 1 ? "it" : "them";
        const choice = await confirmDialog({
            title: "Delete child pins too?",
            message: `This pin has ${n} child pin${plural}.\n\nDelete ${them} as well, or keep ${them} on your map?`,
            confirmLabel: "Delete all",
            altLabel: `Keep child pin${plural}`,
        });
        if (!choice) return false;
        try {
            response = await send(choice === "alt" ? "?children=keep" : "?children=delete");
        } catch {
            return null;
        }
    }

    if (response.ok) {
        // The map keeps its pins in localStorage and its poll only compares the newest pin's `updated` timestamp.
        try {
            localStorage.setItem("ul_pins_dirty", "1");
        } catch {
            /* private mode - a reload still refetches */
        }
        return true;
    }
    return null;
}

// window.confirmDialog is declared in types/globals.d.ts, optional because pages that
// do not load core.js genuinely do not have it.
let linksWatched = false;

export function installGlobalConfirmDialog(): void {
    window.confirmDialog = confirmDialog;
    if (linksWatched) return;
    linksWatched = true;
    document.addEventListener("click", onExternalLinkClick);
}
