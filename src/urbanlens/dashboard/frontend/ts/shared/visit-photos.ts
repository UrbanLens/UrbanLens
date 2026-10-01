/**
 * Photos attached to a visit in the pin page's visit history (``partials/pins/_visit_history.html``) open in the
 * site lightbox. Only the address and caption go in, so it offers none of the pin gallery's photo actions.
 */

import type { LightboxInput } from "./photo-tile";

function photoUrl(thumb: HTMLElement): string {
    // A photo still processing has no file until its tile settles and gains data-url.
    return thumb.dataset.full || thumb.dataset.url || "";
}

function onClick(event: MouseEvent): void {
    const thumb = event.target instanceof Element ? event.target.closest<HTMLElement>(".visit-photo-thumb") : null;
    const visit = thumb?.closest(".visit-photos");
    if (!thumb || !visit) return;
    event.preventDefault();
    const url = photoUrl(thumb);
    if (!url || !window.galleryOpenLightboxItem) return;
    const items: LightboxInput[] = Array.from(visit.querySelectorAll<HTMLElement>(".visit-photo-thumb")).flatMap((el) => {
        const src = photoUrl(el);
        return src ? [{ url: src, caption: el.dataset.caption ?? "" }] : [];
    });
    window.galleryOpenLightboxItem(
        items,
        Math.max(
            items.findIndex((item) => item.url === url),
            0,
        ),
    );
}

export function installVisitPhotos(): void {
    document.addEventListener("click", onClick);
}
