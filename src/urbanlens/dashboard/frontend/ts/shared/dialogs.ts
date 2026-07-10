export interface ConfirmOptions {
    title?: string;
    message?: string;
    confirmLabel?: string;
    cancelLabel?: string;
}

/** Wraps window.confirmDialog (base.html), falling back to native confirm() if unavailable. */
export async function confirmAction(options: ConfirmOptions): Promise<boolean> {
    if (window.confirmDialog) {
        return window.confirmDialog(options);
    }
    return window.confirm(options.message ?? "Are you sure?");
}

export const toast = {
    success(message: string): void {
        window.toastr.success(message);
    },
    error(message: string): void {
        window.toastr.error(message);
    },
    warning(message: string): void {
        window.toastr.warning(message);
    },
    info(message: string): void {
        window.toastr.info(message);
    },
};

/** Re-scans dynamically injected HTML (cloned tree-view nodes, innerHTML swaps) for hx-* attributes. */
export function htmxProcess(element: Element): void {
    window.htmx?.process(element);
}
