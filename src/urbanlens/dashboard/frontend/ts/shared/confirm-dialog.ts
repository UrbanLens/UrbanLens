/**
 * The shared confirm dialog, and the two flows built on it.
 *
 * Installed as window globals because every caller is an inline ``onclick=`` in a
 * template - ``confirmDialog`` alone is reached from ten of them. Lifted out of
 * ``base.html``'s inline script so it is typechecked and testable; the markup it
 * drives (``#confirm-dialog``) still lives in that template.
 *
 * Elements are resolved on first use rather than at load time. That is not a
 * style choice: this bundle loads from ``base.html``'s ``<head>`` (see
 * ``entries-classic/core.ts`` for why it is a classic script rather than a
 * module), and ``#confirm-dialog`` is markup further down the body, so binding
 * eagerly would capture nulls and leave a dialog that never opens.
 */

interface ConfirmOptions {
    title?: string;
    message?: string;
    confirmLabel?: string;
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

    parts = { dialog, ok, alt, title, message };
    document.getElementById("confirm-dialog-cancel")?.addEventListener("click", () => settle(false));
    document.getElementById("confirm-dialog-x")?.addEventListener("click", () => settle(false));
    ok.addEventListener("click", () => settle(true));
    alt.addEventListener("click", () => settle("alt"));
    // Catches every close path at once: backdrop click, Escape, and direct .close().
    dialog.addEventListener("close", () => settle(false));
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

    found.title.textContent = opts.title || "Are you sure?";
    found.message.innerHTML = (opts.message || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/\n/g, "<br>");
    found.ok.textContent = opts.confirmLabel || "Confirm";
    found.ok.className = opts.danger === false ? "btn btn--primary" : "btn--danger-filled";
    (found.alt as HTMLElement & { hidden: boolean }).hidden = !opts.altLabel;
    if (opts.altLabel) found.alt.textContent = opts.altLabel;

    return new Promise<ConfirmResult>((resolve) => {
        resolveCurrent = resolve;
        found.dialog.showModal();
    });
}

/**
 * Confirm before following a community-added link.
 *
 * Returning false from the ``onclick=`` cancels the native navigation; a confirmed
 * click re-opens the url here, because the original click's user activation is
 * already spent by the time the promise resolves.
 */
export function urbanlensConfirmExternalLink(event: Event, url: string): boolean {
    event.preventDefault();
    void confirmDialog({
        title: "Leaving this site",
        message: `This link was added by the community and leads to an external, untrusted site:\n${url}`,
        confirmLabel: "Continue",
        danger: false,
    }).then((ok) => {
        if (ok) window.open(url, "_blank", "noopener");
    });
    return false;
}

/**
 * Delete a pin, letting the server veto with a 409 when it has children so the
 * user decides whether those go too.
 *
 * Returns true (deleted), false (cancelled), or null (the request failed).
 */
export async function deletePinCascade(pinUuid: string, pinName: string, csrfToken: string): Promise<boolean | null> {
    const confirmed = await confirmDialog({
        title: "Delete Pin",
        message: `Delete "${pinName || "this pin"}"?\n\nThe pin and its photos can be restored from Settings → Undo History. Its comments, albums and links cannot.`,
        confirmLabel: "Delete",
    });
    if (!confirmed) return false;

    const url = `/dashboard/rest/pins/${encodeURIComponent(pinUuid)}/`;
    const send = (query: string): Promise<Response> => fetch(url + query, { method: "DELETE", headers: { "X-CSRFToken": csrfToken } });

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
            /* non-JSON 409 */
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
        // The map keeps its pins in localStorage and its poll only compares the newest
        // pin's `updated` timestamp - a deletion cannot advance that, so the poll is
        // blind to it and the map would keep restoring a pin that no longer exists.
        // Flagged here rather than at each call site: the map page remembered to, the
        // pin detail page did not, and the next caller would have had to know as well.
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
declare global {
    interface Window {
        urbanlensConfirmExternalLink?: typeof urbanlensConfirmExternalLink;
        deletePinCascade?: typeof deletePinCascade;
    }
}

export function installGlobalConfirmDialog(): void {
    window.confirmDialog = confirmDialog;
    window.urbanlensConfirmExternalLink = urbanlensConfirmExternalLink;
    window.deletePinCascade = deletePinCascade;
}
