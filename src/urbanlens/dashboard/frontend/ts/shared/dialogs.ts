export interface ConfirmOptions {
    title?: string;
    message?: string;
    confirmLabel?: string;
    cancelLabel?: string;
}

/** Wraps window.confirmDialog (shared/confirm-dialog.ts), falling back to native confirm().
 *
 * Callers here never pass altLabel, so the dialog's third outcome cannot arise - but it
 * is narrowed explicitly rather than assumed, because "alt" is a truthy string and would
 * otherwise read as a confirmation if one were ever added.
 */
export async function confirmAction(options: ConfirmOptions): Promise<boolean> {
    if (window.confirmDialog) {
        return (await window.confirmDialog(options)) === true;
    }
    return window.confirm(options.message ?? "Are you sure?");
}

type ToastKind = "success" | "error" | "warning" | "info";

/** Matches toastr.options.timeOut in dashboard/themes/base.html. */
const FALLBACK_TIMEOUT_MS = 4500;

/** Shows a toast without the library, in the markup toastr itself emits.
 *
 * sass/_toastr.scss is ours and ships in the bundle; only the script is remote, so
 * the same container and class names get the same styling with nothing duplicated.
 *
 * Args:
 *     kind: Which of the four toast styles to render.
 *     message: Text to show. Set as text, never HTML - some callers pass server strings.
 *     title: Optional heading above the message.
 */
function fallbackToast(kind: ToastKind, message: string, title?: string): void {
    const body = document.body;
    if (!body) return;
    let container = document.getElementById("toast-container");
    if (!container) {
        container = document.createElement("div");
        container.id = "toast-container";
        container.className = "toast-bottom-right";
        container.setAttribute("aria-live", "polite");
        body.appendChild(container);
    }

    const item = document.createElement("div");
    item.className = `toast-${kind}`;
    item.setAttribute("role", kind === "error" || kind === "warning" ? "alert" : "status");
    if (title) {
        const heading = document.createElement("div");
        heading.className = "toast-title";
        heading.textContent = title;
        item.appendChild(heading);
    }
    const text = document.createElement("div");
    text.className = "toast-message";
    text.textContent = message;
    item.appendChild(text);
    item.addEventListener("click", () => item.remove());
    container.prepend(item);
    window.setTimeout(() => item.remove(), FALLBACK_TIMEOUT_MS);
}

/** Routes to toastr when it is there, and to our own markup when it is not.
 *
 * toastr is a CDN <script> in dashboard/themes/base.html, so window.toastr is absent
 * whenever that request does not land - offline, blocked, or a failed integrity check.
 * Callers are overwhelmingly error paths, and the network that loses the script is the
 * one that causes the error, so throwing here would take out the recovery around it:
 * the floorplan editor's "could not save" toast sits directly above the call that arms
 * the retry, and a throw left the document dirty with nothing scheduled to try again.
 */
function notify(kind: ToastKind, message: string, title?: string): void {
    const library = window.toastr;
    if (library) library[kind](message, title);
    else fallbackToast(kind, message, title);
}

export const toast = {
    success(message: string, title?: string): void {
        notify("success", message, title);
    },
    error(message: string, title?: string): void {
        notify("error", message, title);
    },
    warning(message: string, title?: string): void {
        notify("warning", message, title);
    },
    info(message: string, title?: string): void {
        notify("info", message, title);
    },
};

/** Re-scans dynamically injected HTML (cloned tree-view nodes, innerHTML swaps) for hx-* attributes. */
export function htmxProcess(element: Element): void {
    window.htmx?.process(element);
}
