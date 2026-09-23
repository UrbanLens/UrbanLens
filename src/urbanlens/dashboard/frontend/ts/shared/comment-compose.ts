/**
 * Small comment-composer behaviours: the reply form toggle, the attached-image filename preview, hover-syncing an activity
 * mention, and swapping in a comment image (or a "Choose Existing" photo) once its re-encode lands.
 */

import { type ProcessingItem, settleProcessingThumb, watchProcessing } from "./photo-processing";

/** Show or hide a comment's reply form, focusing the textarea when it opens. */
export function toggleReplyForm(id: string): void {
    const el = document.getElementById(id);
    if (!el) return;
    el.hidden = !el.hidden;
    if (el.hidden) return;
    // Deferred: focusing an element in the same tick it stops being hidden is
    // unreliable, and the form may still be animating in.
    const textarea = el.querySelector<HTMLTextAreaElement>("textarea");
    if (textarea) setTimeout(() => textarea.focus(), 30);
}

/** Name the chosen file next to the input - the real preview is server-side. */
function onFileChange(event: Event): void {
    const input = event.target;
    if (!(input instanceof HTMLInputElement) || !input.classList.contains("comment-image-input")) return;
    const file = input.files?.[0];
    const preview = input.closest(".comment-compose-actions")?.querySelector<HTMLElement>(".comment-image-preview");
    if (preview) preview.textContent = file ? `📎 ${file.name}` : "";
}

function highlightFromEvent(event: Event, on: boolean): void {
    const target = event.target as HTMLElement | null;
    const link = target?.closest?.(".mention--activity");
    if (!(link instanceof HTMLElement) || !window.tripHighlightMarker) return;
    const id = link.dataset.activityId;
    if (id) window.tripHighlightMarker(id, on);
}

/** Swap a comment image's placeholder for the image, which the link then opens. */
export function settleCommentImage(el: HTMLElement, item: ProcessingItem | null): void {
    settleProcessingThumb(el, item, "comment-image", "full");
    if (el instanceof HTMLAnchorElement && el.dataset.url) el.href = el.dataset.url;
}

const COMMENT_TILES = '.comment-image-link[data-processing="pending"][data-id]';
const PICKER_TILES = '.cip-picker-item[data-processing="pending"][data-id]';

/** Watch the pending comment images and picker photos under *root*; other surfaces' tiles are theirs to watch. */
export function watchCommentImages(root: ParentNode): void {
    const watch = (el: HTMLElement, onSettled: (item: ProcessingItem | null) => void): void => {
        const url = el.closest<HTMLElement>("[data-processing-url]")?.dataset.processingUrl;
        const id = Number(el.dataset.id);
        if (url && id) watchProcessing(url, id, el, onSettled);
    };
    root.querySelectorAll<HTMLElement>(COMMENT_TILES).forEach((el) => watch(el, (item) => settleCommentImage(el, item)));
    root.querySelectorAll<HTMLElement>(PICKER_TILES).forEach((el) => watch(el, (item) => settleProcessingThumb(el, item, "cip-picker-thumb")));
}

declare global {
    interface Window {
        toggleReplyForm?: typeof toggleReplyForm;
        /** Owned by the trip detail page; absent everywhere else. */
        tripHighlightMarker?: (activityId: string, on: boolean) => void;
    }
}

let installed = false;

export function installGlobalCommentCompose(): void {
    // The listeners are delegated from document, so a second install would fire every one twice.
    if (installed) return;
    installed = true;
    // Called from inline onclick= in the comment partials.
    window.toggleReplyForm = toggleReplyForm;

    document.addEventListener("change", onFileChange);
    // Comment panels and the picker arrive by htmx swap; htmx:load also fires for the initial page.
    document.addEventListener("htmx:load", (event) => {
        if (event.target instanceof HTMLElement) watchCommentImages(event.target);
    });
    document.addEventListener("DOMContentLoaded", () => watchCommentImages(document));
    document.addEventListener("mouseover", (e) => highlightFromEvent(e, true));
    document.addEventListener("mouseout", (e) => highlightFromEvent(e, false));
}
