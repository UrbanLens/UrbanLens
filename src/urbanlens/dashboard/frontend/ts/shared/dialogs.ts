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
