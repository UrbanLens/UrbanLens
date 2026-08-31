/**
 * Vault > Documents gallery grid: infinite scroll, off-screen pruning, and the
 * sort control. Same shared engine as Vault Photos (see photo-virtual-grid.ts
 * and vault-photo-grid.ts), parameterized for this page's `.document-tile`
 * markup - a filename + type icon instead of a thumbnail, since a document
 * has no image to decode.
 */

import { bindPhotoGrid } from "./photo-virtual-grid";

interface VaultDocumentJson {
    id?: unknown;
    url?: unknown;
    caption?: unknown;
}

const ICONS_BY_EXTENSION: Record<string, string> = {
    pdf: "picture_as_pdf",
    doc: "description",
    docx: "description",
    odt: "description",
    rtf: "article",
    txt: "article",
    xls: "table_chart",
    xlsx: "table_chart",
    ods: "table_chart",
    csv: "table_chart",
    ppt: "slideshow",
    pptx: "slideshow",
    odp: "slideshow",
};

/** The extension a document's own filename ends in, lowercased and without the dot. */
export function documentExtension(filename: string): string {
    const dot = filename.lastIndexOf(".");
    return dot === -1 ? "" : filename.slice(dot + 1).toLowerCase();
}

export function documentIcon(filename: string): string {
    return ICONS_BY_EXTENSION[documentExtension(filename)] ?? "insert_drive_file";
}

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
    if (!id || !url) return null;
    const caption = String(item.caption ?? "") || "Untitled document";

    const li = document.createElement("li");
    li.className = "document-tile";
    li.id = `document-tile-${id}`;
    li.dataset.id = String(id);
    li.dataset.url = url;
    li.dataset.caption = caption;
    li.innerHTML = TILE_SHELL;

    const icon = li.querySelector(".document-tile-icon");
    if (icon) icon.textContent = documentIcon(caption);
    const name = li.querySelector(".document-tile-name");
    if (name) name.textContent = caption;

    const openBtn = li.querySelector<HTMLButtonElement>(".document-tile-btn");
    if (openBtn) {
        openBtn.setAttribute("aria-label", `Open document: ${caption}`);
        openBtn.addEventListener("click", () => window.documentsOpenLightbox?.(id));
    }
    li.querySelector<HTMLButtonElement>(".document-tile-del")?.addEventListener("click", () => window.documentsDelete?.(id));
    return li;
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
        // No imageSelector: a document tile has no <img> to prune (see
        // TILE_SHELL - an icon + filename, not a decoded bitmap), so
        // off-screen recycling has nothing to do here.
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
    bindGrid(grid, activeSort());
    initSort(grid);
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}

// Exposed so pages/vault/documents.html's own inline upload handler can
// prepend a freshly-uploaded document through the exact same tile markup
// this module renders for fetched pages - see vault-photo-grid.ts's identical
// convention for photos.
window.renderVaultDocumentTile = renderVaultDocumentTile;

window.refreshVaultDocumentGrid = function refreshVaultDocumentGrid(): void {
    const grid = document.getElementById("document-grid");
    if (!grid) return;
    clearLoadedTiles(grid);
    bindGrid(grid, activeSort());
};
