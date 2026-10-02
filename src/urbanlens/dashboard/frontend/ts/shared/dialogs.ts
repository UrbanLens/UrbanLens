import type { ToastrOptions } from "../types/globals";

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

/** Matches toastr.options.timeOut (shared/site-runtime.ts). */
const FALLBACK_TIMEOUT_MS = 4500;

/**
 * Shows a toast without the library, in the markup toastr itself emits.
 */
function fallbackToast(kind: ToastKind, message: string, title?: string): HTMLElement | null {
    const body = document.body;
    if (!body) return null;
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
    return item;
}

/** Open modal dialogs in the order they opened, so the newest is the one on top. */
const openModals: HTMLDialogElement[] = [];

/** The stack while it sits in a dialog: removing the dialog removes it too, and it is put back. */
let hostedStack: HTMLElement | null = null;
let hostWatch: MutationObserver | null = null;

function isModal(dialog: HTMLDialogElement): boolean {
    try {
        return dialog.isConnected && dialog.matches(":modal");
    } catch {
        return false;
    }
}

function topModal(): HTMLDialogElement | null {
    for (let i = openModals.length - 1; i >= 0; i--) {
        const dialog = openModals[i];
        if (dialog && isModal(dialog)) return dialog;
        openModals.splice(i, 1);
    }
    const modals = [...document.querySelectorAll("dialog")].filter(isModal);
    return modals[modals.length - 1] ?? null;
}

function watchHost(stack: HTMLElement, host: HTMLElement): void {
    hostWatch?.disconnect();
    hostWatch = null;
    hostedStack = host === document.body ? null : stack;
    if (!hostedStack || typeof MutationObserver === "undefined") return;
    hostWatch = new MutationObserver(() => {
        if (hostedStack && !hostedStack.isConnected) restoreStack(hostedStack);
    });
    hostWatch.observe(document.body, { childList: true, subtree: true });
}

function restoreStack(stack: HTMLElement): void {
    const replacement = document.getElementById("toast-container");
    if (replacement && replacement !== stack) replacement.prepend(...stack.children);
    else document.body.append(stack);
    raiseToasts();
}

/**
 * Lifts the toast stack into the top layer, above any modal dialog opened since it was last shown, and inside the
 * topmost modal while one is open: everything outside a modal is inert, which would leave the toasts unclickable and
 * unhoverable. Re-shown each time, because a later top-layer entry draws over an earlier one.
 */
export function raiseToasts(): void {
    const container = document.getElementById("toast-container");
    if (!container || typeof container.showPopover !== "function" || !document.body) return;
    container.setAttribute("popover", "manual");
    const host = topModal() ?? document.body;
    if (container.parentElement !== host) host.append(container);
    watchHost(container, host);
    if (container.matches(":popover-open")) container.hidePopover();
    container.showPopover();
}

/** Re-hosts the toast stack whenever a dialog opens or closes. */
export function installToastHosting(): void {
    if (typeof MutationObserver === "undefined" || !document.documentElement) return;
    new MutationObserver((records) => {
        for (const record of records) {
            const dialog = record.target;
            if (dialog instanceof HTMLDialogElement && isModal(dialog) && !openModals.includes(dialog)) openModals.push(dialog);
        }
        if (document.getElementById("toast-container")?.childElementCount) raiseToasts();
    }).observe(document.documentElement, { attributes: true, attributeFilter: ["open"], subtree: true });
}

/**
 * Routes to toastr when it is there, and to our own markup when it is not.
 */
function notify(kind: ToastKind, message: string, title?: string): void {
    const library = window.toastr;
    if (library) library[kind](message, title);
    else {
        fallbackToast(kind, message, title);
        raiseToasts();
    }
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

/**
 * A control at the end of a toast's message: a button carrying data attributes (as dataset keys) for the page's
 * delegated click handler, or a link.
 */
export type ToastAction = { label: string; data: Record<string, string> } | { label: string; href: string };

function toastControl(action: ToastAction): HTMLElement {
    if ("href" in action) {
        const link = document.createElement("a");
        link.href = action.href;
        link.className = "toast-undo-btn";
        link.textContent = action.label;
        return link;
    }
    const button = document.createElement("button");
    button.type = "button";
    button.className = "toast-undo-btn";
    Object.assign(button.dataset, action.data);
    button.textContent = action.label;
    return button;
}

/** The toast element inside the jQuery wrapper toastr hands back. */
function drawnToast(shown: unknown): HTMLElement | null {
    const first: unknown = shown !== null && typeof shown === "object" ? Reflect.get(shown, 0) : null;
    return first instanceof HTMLElement ? first : null;
}

/**
 * A toast whose message ends in a control. toastr escapes the message, so the control is added as a node once the
 * toast is drawn rather than written into the message.
 */
export function toastWithAction(kind: ToastKind, message: string, action: ToastAction, options?: ToastrOptions): void {
    const library = window.toastr;
    const drawn = library ? drawnToast(library[kind](message, undefined, options)) : fallbackToast(kind, message);
    if (!library) raiseToasts();
    drawn?.querySelector(".toast-message")?.append(" ", toastControl(action));
}

/** Re-scans dynamically injected HTML (cloned tree-view nodes, innerHTML swaps) for hx-* attributes. */
export function htmxProcess(element: Element): void {
    window.htmx?.process(element);
}
