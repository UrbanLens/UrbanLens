/**
 * Click-outside dismissal for two small popups that have no other close affordance:
 * the trip member RSVP menu and the photo-album "add to" picker.
 *
 * Neither has anything to do with comments; they were only ever co-located with the
 * reaction picker because all three shared one document click listener in
 * ``base.html``. Splitting them out is behaviour-preserving - independent branches of
 * one listener and separate listeners on the same target run identically - and keeps
 * the reaction picker's module about reactions.
 *
 * The album picker is a ``<details>`` element, so it closes by clearing ``open``
 * rather than setting ``hidden``.
 */

function onDocumentClick(event: MouseEvent): void {
    const target = event.target as HTMLElement | null;
    if (!target?.closest) return;

    if (!target.closest(".trip-member-rsvp-wrap")) {
        document.querySelectorAll<HTMLElement>(".rsvp-popup:not([hidden])").forEach((p) => {
            p.hidden = true;
        });
    }

    if (!target.closest(".pab-add-picker")) {
        document.querySelectorAll<HTMLDetailsElement>(".pab-add-picker[open]").forEach((d) => {
            d.open = false;
        });
    }

    if (!target.closest(".album-menu")) {
        document.querySelectorAll<HTMLDetailsElement>(".album-menu[open]").forEach((d) => {
            d.open = false;
        });
    }
}

export function installGlobalPopupDismiss(): void {
    document.addEventListener("click", onDocumentClick);
}
