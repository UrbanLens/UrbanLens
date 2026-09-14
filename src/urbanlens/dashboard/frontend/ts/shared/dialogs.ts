export interface ConfirmOptions {
    title?: string;
    message?: string;
    confirmLabel?: string;
    cancelLabel?: string;
}

/**
 * Wraps window.confirmDialog (shared/confirm-dialog.ts), falling back to native confirm().
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

/**
 * Shows a toast without the library, in the markup toastr itself emits.
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

/**
 * Routes to toastr when it is there, and to our own markup when it is not.
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
