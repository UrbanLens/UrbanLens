/**
 * Vault > Documents: the document tile, and the page's grid and uploader wired together.
 */

import { FAILED_LABEL, PROCESSING_LABEL, processingPlaceholder, processingStateOf, type ProcessingItem } from "./photo-processing";
import { settleVaultTile, VaultGrid, type VaultKind } from "./vault-media-grid";
import { VaultUploader, type VaultLightboxItem } from "./vault-uploader";

export const DOCUMENTS: VaultKind = { kind: "document", plural: "documents" };

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
    if (processing) {
        li.dataset.processing = processing;
        icon?.replaceWith(processingPlaceholder("document-tile-icon", processing === "failed"));
        if (openBtn) {
            openBtn.disabled = true;
            openBtn.setAttribute("aria-label", `${caption}: ${processing === "failed" ? FAILED_LABEL : PROCESSING_LABEL}`);
        }
        return li;
    }
    openBtn?.setAttribute("aria-label", `Open document: ${caption}`);
    return li;
}

/** Swap a placeholder tile for the settled document, or drop it once the document is gone. */
export function settleVaultDocumentTile(el: HTMLElement, item: ProcessingItem | null): void {
    settleVaultTile(el, item, renderVaultDocumentTile);
}

export function documentLightboxItem(tile: HTMLElement): VaultLightboxItem {
    return {
        url: tile.dataset.url ?? "",
        caption: tile.dataset.caption || "",
        imageId: Number.parseInt(tile.dataset.id ?? "", 10),
        mediaType: "document",
    };
}

/** Wire the Documents page; a no-op on any page without its root. */
export function initVaultDocumentsPage(): void {
    const root = document.getElementById("documents-page");
    if (!root) return;
    // A document tile has no <img> to prune.
    const grid = VaultGrid.find({ kind: DOCUMENTS, renderTile: renderVaultDocumentTile, imageSelector: null });
    grid?.init();

    const uploader = new VaultUploader(root, grid, {
        kind: DOCUMENTS,
        field: "document",
        // The input's `accept` constrains only the picker; a dropped image would upload, be typed a PHOTO, and land in Photos.
        accepts: (file) => !file.type.startsWith("image/"),
        refusedMessage: (refused) =>
            refused.length === 1
                ? `${refused[0]!.name} is an image. Upload it on the Photos page instead.`
                : `${refused.length} images were skipped. Upload those on the Photos page instead.`,
        uploadedMessage: (count) => `${count}${count === 1 ? " document uploaded." : " documents uploaded."}`,
        lightboxItem: documentLightboxItem,
    });
    uploader.bindInputs();
    uploader.bindTileActions();
}
