/**
 * Ambient declarations for globals set up by base.html that TS entry points
 * need to interoperate with. These are intentionally minimal - just the
 * surface actually called from the modules in this project.
 */

interface ToastrOptions {
    timeOut?: number;
    closeButton?: boolean;
    progressBar?: boolean;
}

interface Toastr {
    success(message: string, title?: string, options?: ToastrOptions): void;
    error(message: string, title?: string, options?: ToastrOptions): void;
    warning(message: string, title?: string, options?: ToastrOptions): void;
    info(message: string, title?: string, options?: ToastrOptions): void;
}

interface ConfirmDialogOptions {
    title?: string;
    message?: string;
    confirmLabel?: string;
    cancelLabel?: string;
}

interface HtmxApi {
    process(element: Element): void;
    ajax(verb: string, url: string, options: Record<string, unknown>): void;
}

declare global {
    interface Window {
        toastr: Toastr;
        confirmDialog?: (options: ConfirmDialogOptions) => Promise<boolean>;
        htmx?: HtmxApi;
        csrftoken: string;
    }

    const toastr: Toastr;
    const csrftoken: string;
}

export {};
