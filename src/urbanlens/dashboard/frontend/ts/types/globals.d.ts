/**
 * Ambient declarations for globals set up by base.html that TS entry points
 * need to interoperate with. These are intentionally minimal - just the
 * surface actually called from the modules in this project.
 */
import type { LightboxInput } from "../shared/photo-tile";

interface ToastrOptions {
    timeOut?: number;
    /** How long a hover-paused toast stays after the mouse leaves it. */
    extendedTimeOut?: number;
    closeButton?: boolean;
    progressBar?: boolean;
    /** false requires the close button (or timeout) rather than any click dismissing it. */
    tapToDismiss?: boolean;
    positionClass?: string;
    onHoverTimeOut?: boolean;
    newestOnTop?: boolean;
    showDuration?: number;
    hideDuration?: number;
    /** Render the message as text; toastr's own default is HTML. */
    escapeHtml?: boolean;
}

interface Toastr {
    success(message: string, title?: string, options?: ToastrOptions): void;
    error(message: string, title?: string, options?: ToastrOptions): void;
    warning(message: string, title?: string, options?: ToastrOptions): void;
    info(message: string, title?: string, options?: ToastrOptions): void;
    clear(): void;
    options?: ToastrOptions;
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

export interface HtmxApi {
    process(element: Element): void;
    /**
 * Dispatch an htmx event on an element - used to fire `ul:unhide` on sections whose hx-get was skipped while they were collapsed.
 */
    trigger(element: Element, event: string, detail?: unknown): void;
    ajax(verb: string, url: string, options: Record<string, unknown>): Promise<void>;
}

interface UlBulkToolbar {
    sync(namespace: string, count: number, actions: Record<string, (() => void) | null | undefined>): void;
    clear(namespace: string): void;
}

/** A photo as the page's map pins it. */
export interface GalleryMarkerImage {
    id: number;
    url: string;
    /** Tiny map-marker preview; absent for a row that hasn't been generated one yet. */
    marker_thumb_url?: string;
    latitude: number | null;
    longitude: number | null;
}

export interface CommentMapComposerOptions {
    form?: HTMLElement;
    context?: { pinSlug?: string; locationSlug?: string } | null;
    onSaved?: (uuid: string) => void;
    // Initial center/zoom for a brand-new map (e.g. the live view of the page's main map when the user clicks "take a screenshot").
    initialView?: { lat: number; lng: number; zoom?: number } | null;
    /** Seeds the composer with a copy of this map snapshot; the original is never changed. */
    existingData?: unknown;
    /** Adds a "Choose Existing" tab listing the maps *fetchUrl* returns. */
    existingMapPicker?: { fetchUrl: string; onPick: (uuid: string) => void };
    startTab?: "draw" | "existing";
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
        // Sizes an edit-in-place input to the text it replaces (themes/base.html).
        urbanlensSizeEditInPlaceInput: (displayEl: Element, inputEl: HTMLElement) => void;
        // The shared map composer dialog (base.html).
        _openCommentMapComposer: (formOrOptions: HTMLElement | CommentMapComposerOptions) => void;
        _clearCommentMap?: (form: HTMLElement) => void;
        // Where the composer starts when opened for a form (static/js/comment-map.js reads them).
        _commentMapDefaultLat?: number;
        _commentMapDefaultLng?: number;
        // static/js/comment-map.js: a small non-interactive map of a snapshot in *el*, and the page-wide pass that renders every .comment-map-thumb.
        _renderMapThumb?: (el: HTMLElement, data: unknown, refLatLng: null) => { remove(): void } | null;
        _initThumbs?: () => void;
        _expandCommentMap?: (commentId: string) => void;
        // Adds an external Media-gallery item to an album.
        albumAddExternalMedia?: (addUrl: string, media: { source: string; url: string; page_url?: string; caption?: string }) => Promise<void>;
        galleryOpenLightboxItem?: (list: LightboxInput[], idx: number) => void;
        // static/js/media-thumb-fallback.js, loaded in <head> by themes/base.html.
        urbanlensMediaThumbFallback?: (img: HTMLImageElement, icon?: string, className?: string) => void;
        // Defined by shared/media-lightbox.ts, exposed by entries/map-annotations.ts (loaded identically by the pin and wiki pages).
        mediaOpenLightbox?: (thumbBtn: HTMLElement) => void;
        // Set by shared/vault-photo-grid.ts and shared/vault-document-grid.ts for the tile partials' inline handlers.
        photosOpenLightbox?: (imageId: number) => void;
        photosDelete?: (imageId: number) => void;
        documentsOpenLightbox?: (imageId: number) => void;
        documentsDelete?: (imageId: number) => void;
        // Set by shared/photo-pin-confirm.ts for the organize queue cards' "Create pin" buttons.
        photosLoadPinConfirm?: (url: string) => void;
        // Set by shared/photo-gallery.ts (else shared/album-items.ts), for the page's map and lightbox.
        galleryOpenLightbox?: (imgId: number, fallback?: { url: string; caption?: string }) => void;
        galleryRepositionImage?: (imgId: number, lat: number, lng: number, onRejected?: () => void) => void;
        gallerySetPhotoMapHidden?: (imgId: number, hidden: boolean, onRejected?: () => void) => void;
        photosToggleSelectMode?: () => void;
        // Set by entries/map-annotations.ts, where the page has a map.
        _galleryAddMarker?: (img: GalleryMarkerImage) => void;
        _galleryRemoveMarker?: (imgId: number) => void;
        _galleryHighlightMarker?: (imgId: number, on: boolean) => void;
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
