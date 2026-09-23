/**
 * Vault > Photos gallery grid: infinite scroll, off-screen image pruning, and the sort control.
 */

import { observeProcessingTiles, PROCESSING_LABEL, FAILED_LABEL, processingPlaceholder, processingStateOf, type ProcessingItem } from "./photo-processing";
import { bindPhotoGrid } from "./photo-virtual-grid";

interface VaultPhotoJson {
    id?: unknown;
    url?: unknown;
    thumb_url?: unknown;
    caption?: unknown;
    author?: unknown;
    copyright?: unknown;
    source_url?: unknown;
    taken_at?: unknown;
    latitude?: unknown;
    longitude?: unknown;
    processing?: unknown;
    processing_failed?: unknown;
}

// The static shell has no interpolated values at all.
const TILE_SHELL =
    '<button type="button" class="photo-tile-btn"><img alt="" loading="lazy" onload="this.classList.add(\'is-loaded\')" ' +
    "onerror=\"urbanlensMediaThumbFallback(this, 'broken_image', 'photo-tile-fallback')\"></button>" +
    '<button type="button" class="photo-tile-del" title="Delete photo"><i class="material-symbols-outlined">delete</i></button>';

/** Build one grid tile, matching the markup `_photo_grid.html` renders server-side. */
export function renderVaultPhotoTile(raw: Record<string, unknown>): HTMLElement | null {
    const item = raw as VaultPhotoJson;
    const id = Number(item.id);
    const url = String(item.url ?? "");
    const thumbUrl = String(item.thumb_url || url);
    const processing = processingStateOf(raw);
    if (!id || (!url && !thumbUrl && !processing)) return null;
    const caption = String(item.caption ?? "");

    const li = document.createElement("li");
    li.className = "photo-tile";
    li.id = `photo-tile-${id}`;
    li.dataset.id = String(id);
    li.dataset.url = url;
    li.dataset.thumbUrl = thumbUrl;
    li.dataset.caption = caption;
    li.dataset.author = String(item.author ?? "");
    li.dataset.copyright = String(item.copyright ?? "");
    li.dataset.sourceUrl = String(item.source_url ?? "");
    li.dataset.takenAt = String(item.taken_at ?? "");
    li.innerHTML = TILE_SHELL;

    if (item.latitude != null && item.longitude != null) {
        const badge = document.createElement("span");
        badge.className = "photo-tile-badge";
        badge.title = "Has location";
        badge.innerHTML = '<i class="material-symbols-outlined">place</i>';
        li.querySelector(".photo-tile-btn")?.appendChild(badge);
    }

    const openBtn = li.querySelector<HTMLButtonElement>(".photo-tile-btn");
    li.querySelector<HTMLButtonElement>(".photo-tile-del")?.addEventListener("click", () => window.photosDelete?.(id));
    const img = li.querySelector("img");
    if (processing) {
        li.dataset.processing = processing;
        img?.replaceWith(processingPlaceholder("photo-tile-fallback", processing === "failed"));
        if (openBtn) {
            openBtn.disabled = true;
            openBtn.setAttribute("aria-label", `${caption || "Photo"}: ${processing === "failed" ? FAILED_LABEL : PROCESSING_LABEL}`);
        }
        return li;
    }
    if (openBtn) {
        openBtn.setAttribute("aria-label", `Open photo: ${caption || "untitled"}`);
        openBtn.addEventListener("click", () => window.photosOpenLightbox?.(id));
    }
    if (img) img.src = thumbUrl;
    return li;
}

/** Swap a placeholder tile for the settled photo, and re-read the organize queue it may now belong in. */
function settleVaultTile(el: HTMLElement, item: ProcessingItem | null): void {
    const next = item ? renderVaultPhotoTile(item) : null;
    if (next) el.replaceWith(next);
    else el.remove();
    requestQueueRefresh();
}

let queueRefresh: ReturnType<typeof setTimeout> | null = null;

function requestQueueRefresh(): void {
    if (queueRefresh !== null) return;
    queueRefresh = setTimeout(() => {
        queueRefresh = null;
        document.body.dispatchEvent(new Event("refreshQueue"));
    }, 500);
}

export function renderVaultSkeletonTile(): HTMLElement {
    const li = document.createElement("li");
    li.className = "photo-tile photo-tile--skeleton";
    li.setAttribute("aria-hidden", "true");
    return li;
}

const SKELETON_COUNT = 6;

let unbindGrid: (() => void) | null = null;

function clearLoadedTiles(grid: HTMLElement): void {
    grid.querySelectorAll(".photo-tile[data-id]").forEach((el) => el.remove());
    grid.querySelectorAll(".photo-grid-sentinel").forEach((el) => el.remove());
}

function bindGrid(grid: HTMLElement, sort: string): void {
    if (unbindGrid) {
        unbindGrid();
        unbindGrid = null;
    }
    // Read from the grid's own dataset (set server-side from the ?show= the page was loaded with), not the URL directly.
    const show = grid.dataset.show === "from_others" ? "from_others" : "";
    unbindGrid = bindPhotoGrid(grid, {
        inAlbum: false,
        itemSelector: ".photo-tile[data-id]",
        imageSelector: ".photo-tile img",
        renderTile: renderVaultPhotoTile,
        extraParams: show ? { sort, show } : { sort },
        skeletonCount: SKELETON_COUNT,
        renderSkeleton: renderVaultSkeletonTile,
    });
}

function activeSort(): string {
    const select = document.getElementById("vault-photos-sort");
    return select instanceof HTMLSelectElement ? select.value : "recent";
}

function initSort(grid: HTMLElement): void {
    const select = document.getElementById("vault-photos-sort");
    if (!(select instanceof HTMLSelectElement)) return;
    select.addEventListener("change", () => {
        clearLoadedTiles(grid);
        bindGrid(grid, select.value);
    });
}

function init(): void {
    const queue = document.getElementById("photos-attention-wrap");
    // A queue card holds no photo JSON to re-render from; the queue re-renders itself.
    if (queue) observeProcessingTiles(queue, () => requestQueueRefresh());
    const grid = document.getElementById("photo-grid");
    if (!grid) return;
    observeProcessingTiles(grid, settleVaultTile);
    bindGrid(grid, activeSort());
    initSort(grid);
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}

// Exposed so pages/vault/photos.html's own inline upload handler can prepend a freshly-uploaded photo through the exact same tile markup.
window.renderVaultPhotoTile = renderVaultPhotoTile;

// Re-fetches the grid from scratch under the current sort.
window.refreshVaultPhotoGrid = function refreshVaultPhotoGrid(): void {
    const grid = document.getElementById("photo-grid");
    if (!grid) return;
    clearLoadedTiles(grid);
    bindGrid(grid, activeSort());
};
