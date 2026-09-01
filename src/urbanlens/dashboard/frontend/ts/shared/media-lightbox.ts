/**
 * Opens the shared photo lightbox (`_photo_lightbox.html`) from a Media-gallery
 * tile (`pin_media_items.html`) - the combined external-provider + own-photos
 * gallery shared by the Private Pin page and the location wiki page.
 *
 * Both pages render that same tile partial into their own grid container
 * (`#media-gallery-grid` on the pin page, `#wiki-media-grid` on the wiki page)
 * - this reads the containing grid by its shared `.media-gallery-grid` class
 * instead of either page's id, so one function serves both without either
 * page needing to tell it which id to use. Was previously duplicated as an
 * inline, untyped `<script>` in `pages/location/index.html` alone - the wiki
 * page never had its own copy, so clicking a Media-gallery photo there threw
 * `window.mediaOpenLightbox is not a function` and silently did nothing (see
 * docs/PROBLEMS.md, 2026-09-01).
 */

import type { LightboxItem } from "./photo-tile";

function parseRelevant(raw: string | undefined): boolean | null {
    if (raw === "true") return true;
    if (raw === "false") return false;
    return null;
}

function parseIsMine(raw: string | undefined): boolean {
    // Absent (not "true"/"false") means this tile carries no ownership data at
    // all - an external-provider result, never materialized into a real Image
    // row. _setLightboxMeta's own "isMine !== false" convention already treats
    // that as "assume mine" (matching every other page whose item shape
    // doesn't set it), so default to true here rather than guessing false.
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
        // Only set once an item has been materialized (mediaSetRelevance/
        // wikiMediaVote) - enables "Set as cover photo"/"Copy to my Private
        // Pin" for a marked item exactly like a real upload, since by then it
        // genuinely is one.
        imageId: el.dataset.imageId ? Number.parseInt(el.dataset.imageId, 10) : null,
        uuid: "",
        isMine: parseIsMine(el.dataset.mine),
        // Your own photos aren't "relevant"-markable (see pin_media_items.html's
        // source_key == "photos" branch) - manage those from the gallery's own
        // "Mine"/"Manage" tab instead. Also off entirely on a page that has no
        // relevance endpoint to post to (the wiki's Media section votes per-tile
        // instead - see wikiMediaVote in wiki.html) - otherwise the lightbox's
        // thumbs-up/down would render as if clickable and silently no-op.
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
 * Open the shared lightbox on the Media-gallery item `thumbBtn` belongs to,
 * with every other currently-visible item in the same grid as prev/next
 * navigation - mirrors `galleryOpenLightbox`'s (`_photo_gallery.html`) same
 * shape for the Photos-tab gallery.
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
