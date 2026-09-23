/**
 * Vault > Documents gallery grid: infinite scroll, off-screen pruning, and the sort control.
 */

import { FAILED_LABEL, observeProcessingTiles, PROCESSING_LABEL, processingPlaceholder, processingStateOf, type ProcessingItem } from "./photo-processing";
import { bindPhotoGrid } from "./photo-virtual-grid";

interface VaultDocumentJson {
    id?: unknown;
    url?: unknown;
    caption?: unknown;
    document_icon?: unknown;
}

/** Fallback only - the icon normally arrives with the item (see image_to_gallery_json). */
const DEFAULT_DOCUMENT_ICON = "insert_drive_file";

const TILE_SHELL =
    '<button type="button" class="document-tile-btn">' +
    '<i class="material-symbols-outlined document-tile-icon"></i>' +
    '<span class="document-tile-name"></span>' +
    "</button>" +
    '<button type="button" class="document-tile-del" title="Delete document"><i class="material-symbols-outlined">delete</i></button>';

/** Build one grid tile, matching the markup `_document_grid.html` renders server-side. */
export function renderVaultDocumentTile(raw: Record<string, unknown>): HTMLElement | null {
    const item = raw as VaultDocumentJson;
    const id = Number(item.id);
    const url = String(item.url ?? "");
    const processing = processingStateOf(raw);
    if (!id || (!url && !processing)) return null;
    const caption = String(item.caption ?? "") || "Untitled document";

    const li = document.createElement("li");
    li.className = "document-tile";
    li.id = `document-tile-${id}`;
    li.dataset.id = String(id);
    li.dataset.url = url;
    li.dataset.caption = caption;
    li.innerHTML = TILE_SHELL;

    const icon = li.querySelector(".document-tile-icon");
    if (icon) icon.textContent = String(item.document_icon || "") || DEFAULT_DOCUMENT_ICON;
    const name = li.querySelector(".document-tile-name");
    if (name) name.textContent = caption;

    const openBtn = li.querySelector<HTMLButtonElement>(".document-tile-btn");
    li.querySelector<HTMLButtonElement>(".document-tile-del")?.addEventListener("click", () => window.documentsDelete?.(id));
    if (processing) {
        li.dataset.processing = processing;
        icon?.replaceWith(processingPlaceholder("document-tile-icon", processing === "failed"));
        if (openBtn) {
            openBtn.disabled = true;
            openBtn.setAttribute("aria-label", `${caption}: ${processing === "failed" ? FAILED_LABEL : PROCESSING_LABEL}`);
        }
        return li;
    }
    if (openBtn) {
        openBtn.setAttribute("aria-label", `Open document: ${caption}`);
        openBtn.addEventListener("click", () => window.documentsOpenLightbox?.(id));
    }
    return li;
}

/** Swap a placeholder tile for the settled document, or drop it once the document is gone. */
export function settleVaultDocumentTile(el: HTMLElement, item: ProcessingItem | null): void {
    const next = item ? renderVaultDocumentTile(item) : null;
    if (next) el.replaceWith(next);
    else el.remove();
}

export function renderVaultDocumentSkeletonTile(): HTMLElement {
    const li = document.createElement("li");
    li.className = "document-tile document-tile--skeleton";
    li.setAttribute("aria-hidden", "true");
    return li;
}

const SKELETON_COUNT = 6;

let unbindGrid: (() => void) | null = null;

function clearLoadedTiles(grid: HTMLElement): void {
    grid.querySelectorAll(".document-tile[data-id]").forEach((el) => el.remove());
    grid.querySelectorAll(".photo-grid-sentinel").forEach((el) => el.remove());
}

function bindGrid(grid: HTMLElement, sort: string): void {
    if (unbindGrid) {
        unbindGrid();
        unbindGrid = null;
    }
    unbindGrid = bindPhotoGrid(grid, {
        inAlbum: false,
        itemSelector: ".document-tile[data-id]",
        // A document tile has no <img> to prune.
        imageSelector: null,
        renderTile: renderVaultDocumentTile,
        extraParams: { sort },
        skeletonCount: SKELETON_COUNT,
        renderSkeleton: renderVaultDocumentSkeletonTile,
    });
}

function activeSort(): string {
    const select = document.getElementById("vault-documents-sort");
    return select instanceof HTMLSelectElement ? select.value : "recent";
}

function initSort(grid: HTMLElement): void {
    const select = document.getElementById("vault-documents-sort");
    if (!(select instanceof HTMLSelectElement)) return;
    select.addEventListener("change", () => {
        clearLoadedTiles(grid);
        bindGrid(grid, select.value);
    });
}

function init(): void {
    const grid = document.getElementById("document-grid");
    if (!grid) return;
    observeProcessingTiles(grid, settleVaultDocumentTile);
    bindGrid(grid, activeSort());
    initSort(grid);
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}

// Exposed so pages/vault/documents.html's own inline upload handler can prepend a freshly-uploaded document through the exact same tile.
window.renderVaultDocumentTile = renderVaultDocumentTile;

window.refreshVaultDocumentGrid = function refreshVaultDocumentGrid(): void {
    const grid = document.getElementById("document-grid");
    if (!grid) return;
    clearLoadedTiles(grid);
    bindGrid(grid, activeSort());
};
