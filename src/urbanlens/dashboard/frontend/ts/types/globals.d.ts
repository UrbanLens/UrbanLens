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

interface UlBulkToolbar {
    sync(namespace: string, count: number, actions: Record<string, (() => void) | null | undefined>): void;
    clear(namespace: string): void;
}

interface CommentMapComposerOptions {
    form?: HTMLElement;
    context?: { pinSlug?: string; locationSlug?: string } | null;
    onSaved?: (uuid: string) => void;
    // Initial center/zoom for a brand-new map (e.g. the live view of the page's main
    // map when the user clicks "take a screenshot"). Takes priority over the stale
    // window._commentMapDefaultLat/Lng globals and the hardcoded Manhattan fallback.
    initialView?: { lat: number; lng: number; zoom?: number } | null;
}

declare global {
    interface Window {
        toastr: Toastr;
        confirmDialog?: (options: ConfirmDialogOptions) => Promise<boolean>;
        htmx?: HtmxApi;
        ulBulkToolbar?: UlBulkToolbar;
        csrftoken: string;
        // The shared map composer dialog (base.html) - opened with a host form
        // element (legacy comment/visit/trip-comment usage) or an options
        // object with no form, which switches it into standalone save mode.
        _openCommentMapComposer: (formOrOptions: HTMLElement | CommentMapComposerOptions) => void;
    }

    const toastr: Toastr;
    const csrftoken: string;
}

// Leaflet is loaded globally via a CDN <script> tag (not bundled) on
// map/pin-detail/wiki/safety pages. @types/leaflet's own `export as
// namespace L` (activated via tsconfig's `types: ["leaflet"]`) already
// provides the global `L` namespace/value - nothing further needed here.

export {};
