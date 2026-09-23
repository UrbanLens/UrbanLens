/**
 * The pin Photos tab's "From public sources" grid: the Media panel's external photos, paged and attributed.
 */

import { bindPhotoGrid } from "./photo-virtual-grid";

export interface ExternalPhoto {
    source: string;
    sourceName: string;
    key: string;
    url: string;
    thumbUrl: string;
    caption: string;
    author: string;
    pageUrl: string;
}

/** One `?external=1` item, or null when it has nothing to show. */
export function externalPhotoFromJson(raw: Record<string, unknown>): ExternalPhoto | null {
    const key = String(raw.key ?? "");
    const thumbUrl = String(raw.thumb_url ?? "");
    if (!key || !thumbUrl) return null;
    return {
        source: String(raw.source ?? ""),
        sourceName: String(raw.source_name ?? ""),
        key,
        url: String(raw.url ?? ""),
        thumbUrl,
        caption: String(raw.caption ?? ""),
        author: String(raw.author ?? ""),
        pageUrl: String(raw.page_url ?? ""),
    };
}

/** A grid tile: the album tile's shape, keyed by media key instead of photo id, with its source named on it. */
export function renderExternalPhotoTile(photo: ExternalPhoto): HTMLLIElement {
    const li = document.createElement("li");
    li.className = "gallery-item gallery-item--external";
    li.dataset.mediaSource = photo.source;
    li.dataset.mediaKey = photo.key;
    li.dataset.url = photo.url;
    li.dataset.thumbUrl = photo.thumbUrl;
    li.dataset.caption = photo.caption;
    li.dataset.author = photo.author;
    li.dataset.sourceUrl = photo.pageUrl;
    li.dataset.sourceName = photo.sourceName;
    li.innerHTML =
        `<button type="button" class="gallery-thumb-btn" data-photo-open><img alt="" class="gallery-thumb" loading="lazy" decoding="async"><span class="gallery-child-ribbon"></span></button>` +
        `<a class="gallery-label-btn" target="_blank" rel="noopener noreferrer" title="Open source"><i class="material-symbols-outlined">open_in_new</i></a>` +
        (photo.caption ? `<p class="album-item-caption"></p>` : "");

    const img = li.querySelector("img");
    if (img) {
        img.dataset.guarded = "1";
        img.alt = photo.caption || photo.sourceName || "Photo";
        img.addEventListener("error", () => window.urbanlensMediaThumbFallback?.(img, "broken_image", "gallery-thumb gallery-thumb--placeholder"));
        img.src = photo.thumbUrl;
    }
    const ribbon = li.querySelector<HTMLElement>(".gallery-child-ribbon");
    if (ribbon) {
        ribbon.textContent = photo.sourceName;
        ribbon.title = photo.author ? `${photo.sourceName} - ${photo.author}` : photo.sourceName;
    }
    const link = li.querySelector<HTMLAnchorElement>(".gallery-label-btn");
    if (link) {
        if (photo.pageUrl) link.href = photo.pageUrl;
        else link.remove();
    }
    const caption = li.querySelector(".album-item-caption");
    if (caption) caption.textContent = photo.caption;
    return li;
}

function renderFromJson(raw: Record<string, unknown>): HTMLElement | null {
    const photo = externalPhotoFromJson(raw);
    return photo ? renderExternalPhotoTile(photo) : null;
}

const bound = new Map<HTMLElement, () => void>();

/** Page every `[data-external-photo-grid]` under *root* not already paging, and let go of grids a swap removed. */
export function bindExternalPhotoGrids(root: ParentNode = document): void {
    for (const [grid, unbind] of bound) {
        if (grid.isConnected) continue;
        unbind();
        bound.delete(grid);
    }
    root.querySelectorAll<HTMLElement>("[data-external-photo-grid]").forEach((grid) => {
        if (bound.has(grid)) return;
        bound.set(grid, bindPhotoGrid(grid, { inAlbum: false, itemSelector: ".gallery-item[data-media-key]", renderTile: renderFromJson }));
    });
}
