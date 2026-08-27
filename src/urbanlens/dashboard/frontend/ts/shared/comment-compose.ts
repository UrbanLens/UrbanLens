/**
 * Small comment-composer behaviours: the reply form toggle, the attached-image
 * filename preview, and hover-syncing an activity mention with the trip map.
 *
 * ``tripHighlightMarker`` is consumed here but owned by the trip detail page - the
 * guard is what keeps these mentions inert on the pin and wiki pages, where the same
 * comment markup renders but no trip map exists.
 *
 * Ported out of ``base.html``'s inline script unchanged.
 */

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

declare global {
    interface Window {
        toggleReplyForm?: typeof toggleReplyForm;
        /** Owned by the trip detail page; absent everywhere else. */
        tripHighlightMarker?: (activityId: string, on: boolean) => void;
    }
}

export function installGlobalCommentCompose(): void {
    // Called from inline onclick= in the comment partials.
    window.toggleReplyForm = toggleReplyForm;

    document.addEventListener("change", onFileChange);
    document.addEventListener("mouseover", (e) => highlightFromEvent(e, true));
    document.addEventListener("mouseout", (e) => highlightFromEvent(e, false));
}
