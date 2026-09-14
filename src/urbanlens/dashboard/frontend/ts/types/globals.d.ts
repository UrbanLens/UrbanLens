/**
 * Ambient declarations for globals set up by base.html that TS entry points
 * need to interoperate with. These are intentionally minimal - just the
 * surface actually called from the modules in this project.
 */
import type { LightboxItem } from "../shared/photo-tile";

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
    /** Shows a third button; picking it resolves with "alt" rather than a boolean. */
    altLabel?: string;
    /** false renders the primary button as non-destructive. */
    danger?: boolean;
}

interface HtmxApi {
    process(element: Element): void;
    /**
 * Dispatch an htmx event on an element - used to fire `ul:unhide` on sections whose hx-get was skipped while they were collapsed.
 */
    trigger(element: Element, event: string, detail?: unknown): void;
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
    // Initial center/zoom for a brand-new map (e.g. the live view of the page's main map when the user clicks "take a screenshot").
    initialView?: { lat: number; lng: number; zoom?: number } | null;
}

declare global {
    interface Window {
        // A CDN <script> in dashboard/themes/base.html, so it is absent whenever that request does not land. shared/dialogs.ts's toast falls.
        toastr?: Toastr;
        // Resolves "alt" when the caller offered altLabel and the user picked it.
        confirmDialog?: (options: ConfirmDialogOptions | string) => Promise<boolean | "alt">;
        htmx?: HtmxApi;
        ulBulkToolbar?: UlBulkToolbar;
        csrftoken: string;
        // The shared map composer dialog (base.html).
        _openCommentMapComposer: (formOrOptions: HTMLElement | CommentMapComposerOptions) => void;
        // Adds an external Media-gallery item to an album.
        albumAddExternalMedia?: (addUrl: string, media: { source: string; url: string; page_url?: string; caption?: string }) => Promise<void>;
        galleryOpenLightboxItem?: (list: LightboxItem[], idx: number) => void;
        // Defined by shared/media-lightbox.ts, exposed by entries/map-annotations.ts (loaded identically by the pin and wiki pages).
        mediaOpenLightbox?: (thumbBtn: HTMLElement) => void;
        // Defined by pages/vault/photos.html's own inline script (upload/delete/ lightbox are plain page JS, not a module).
        photosOpenLightbox?: (imageId: number) => void;
        photosDelete?: (imageId: number) => void;
        // Defined by shared/vault-photo-grid.ts; called from pages/vault/photos.html's own upload handler so a freshly-uploaded tile is built.
        renderVaultPhotoTile?: (raw: Record<string, unknown>) => HTMLElement | null;
        // Re-fetches the Vault Photos grid from scratch under the current sort.
        refreshVaultPhotoGrid?: () => void;
        // Vault Documents' equivalents of the four above - see
        // pages/vault/documents.html and shared/vault-document-grid.ts.
        documentsOpenLightbox?: (imageId: number) => void;
        documentsDelete?: (imageId: number) => void;
        renderVaultDocumentTile?: (raw: Record<string, unknown>) => HTMLElement | null;
        refreshVaultDocumentGrid?: () => void;
        gallerySetPhotoMapHidden?: (imgId: number, hidden: boolean, onRejected?: () => void) => void;
        _galleryRemoveMarker?: (imgId: number) => void;
        _albumSyncMapHidden?: (imgId: number, hidden: boolean) => void;
        // Georeferenced map image overlays.
        ulMapOverlayStartAlign?: (uuid: string) => void;
        ulMapOverlayPreviewOpacity?: (uuid: string, value: string) => void;
        ulMapOverlaySeedCorners?: () => void;
        ulMapOverlayPickFromMedia?: (galleryJsonUrl?: string) => void;
        ulMapOverlayChooseImage?: (id: number, caption: string) => void;
        ulMapOverlaySyncSubmitState?: () => void;
        ulMapOverlayChooseFile?: () => void;
        ulMapOverlayChooseUrl?: () => void;
        ulMapOverlayHandleDrop?: (event: DragEvent, zone: HTMLElement) => void;
        // The current user's keyboard-shortcut overrides (Settings > Shortcuts), rendered server-side by base.html via.
        UL_HOTKEYS?: Record<string, string>;
        // Wikipedia-style page tabs (static/js/page-tabs.js, not bundled).
        ulActivatePageTab?: (name: string, options?: { skipHash?: boolean }) => void;
    }

    const toastr: Toastr;
    const csrftoken: string;
}

// Leaflet is loaded globally via a CDN <script> tag (not bundled) on map/pin-detail/wiki/safety pages. @types/leaflet's own `export.

export {};
