/**
 * Click-outside dismissal for two small popups that have no other close affordance.
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
