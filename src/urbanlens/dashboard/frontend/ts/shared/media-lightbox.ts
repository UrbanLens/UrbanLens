/**
 * Opens the shared photo lightbox (`_photo_lightbox.html`) from a Media-gallery tile (`pin_media_items.html`).
 */

import type { LightboxItem } from "./photo-tile";

function parseRelevant(raw: string | undefined): boolean | null {
    if (raw === "true") return true;
    if (raw === "false") return false;
    return null;
}

function parseIsMine(raw: string | undefined): boolean {
    // Absent (not "true"/"false") means this tile carries no ownership data at all.
    return raw !== "false";
}

function parseNumberOrNull(raw: string | undefined): number | null {
    if (!raw) return null;
    const value = Number.parseFloat(raw);
    return Number.isFinite(value) ? value : null;
}

/** Build one Media-gallery tile's normalized lightbox item from its dataset. */
function mediaLightboxItemFromElement(el: HTMLElement, relevanceEnabled: boolean): LightboxItem {
    const mediaSource = el.dataset.mediaSource ?? "";
    return {
        url: el.dataset.mediaUrl ?? "",
        // Fallback for a full-res file the browser can't render (e.g. a
        // Wikimedia .tif result) - see _photo_lightbox.html.
        thumbUrl: el.dataset.mediaThumb || "",
        caption: el.dataset.mediaCaption || "",
        author: el.dataset.mediaAuthor || "",
        copyright: "",
        sourceUrl: el.dataset.mediaPageUrl || el.dataset.mediaUrl || "",
        sourceName: el.dataset.mediaSourceName || "",
        takenAt: "",
        // Only set once an item has been materialized (mediaSetRelevance/ wikiMediaVote).
        imageId: el.dataset.imageId ? Number.parseInt(el.dataset.imageId, 10) : null,
        uuid: "",
        isMine: parseIsMine(el.dataset.mine),
        // Your own photos aren't "relevant"-markable - manage those from the gallery's own "Mine"/"Manage" tab instead.
        canRelevance: relevanceEnabled && mediaSource !== "photos",
        relevant: parseRelevant(el.dataset.mediaRelevant),
        latitude: parseNumberOrNull(el.dataset.lat),
        longitude: parseNumberOrNull(el.dataset.lng),
        mapHidden: false,
        copiedFromLabel: el.dataset.copiedFromLabel || "",
        mediaSource,
        mediaKey: el.dataset.mediaKey ?? "",
    };
}

/**
 * Open the shared lightbox on the Media-gallery item `thumbBtn` belongs to, with every other currently-visible item in the same grid.
 */
export function openMediaLightbox(thumbBtn: HTMLElement): void {
    const itemEl = thumbBtn.closest<HTMLElement>(".media-item");
    const grid = itemEl?.closest<HTMLElement>(".media-gallery-grid");
    if (!itemEl || !grid) return;

    const relevanceEnabled = !!grid.dataset.relevanceUrl;
    const visible = Array.from(grid.querySelectorAll<HTMLElement>(".media-item")).filter((el) => !el.classList.contains("media-tab-excluded"));
    const list = visible.map((el) => mediaLightboxItemFromElement(el, relevanceEnabled));
    const idx = visible.indexOf(itemEl);
    window.galleryOpenLightboxItem?.(list, idx < 0 ? 0 : idx);
}
