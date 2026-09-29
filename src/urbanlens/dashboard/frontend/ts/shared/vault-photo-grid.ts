/**
 * Vault > Photos: the photo tile, and the page's grid, uploader and organize queue wired together.
 */

import { FAILED_LABEL, observeProcessingTiles, PROCESSING_LABEL, processingPlaceholder, processingStateOf } from "./photo-processing";
import { VaultGrid, type VaultKind } from "./vault-media-grid";
import { bindUploadRetry, VaultUploader, type VaultLightboxItem } from "./vault-uploader";

export const PHOTOS: VaultKind = { kind: "photo", plural: "photos" };

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

export function photoLightboxItem(tile: HTMLElement): VaultLightboxItem {
    const d = tile.dataset;
    return {
        url: d.url ?? "",
        thumbUrl: d.thumbUrl || "",
        caption: d.caption || "",
        author: d.author || "",
        copyright: d.copyright || "",
        sourceUrl: d.sourceUrl || "",
        sourceName: "",
        takenAt: d.takenAt || "",
        imageId: Number.parseInt(d.id ?? "", 10),
        canRelevance: false,
        relevant: null,
    };
}

let queueRefresh: ReturnType<typeof setTimeout> | null = null;

/** Ask the organize queue to re-render, at most once per half second. */
function requestQueueRefresh(): void {
    if (queueRefresh !== null) return;
    queueRefresh = setTimeout(() => {
        queueRefresh = null;
        document.body.dispatchEvent(new Event("refreshQueue"));
    }, 500);
}

function refreshQueueAfter(delay: number): void {
    setTimeout(() => document.body.dispatchEvent(new Event("refreshQueue")), delay);
}

/** Wire the Photos page; a no-op on any page without its root. */
export function initVaultPhotosPage(): void {
    const root = document.getElementById("photos-page");
    if (!root) return;
    const queue = document.getElementById("photos-attention-wrap");
    // A queue card holds no photo JSON to re-render from; the queue re-renders itself.
    if (queue) observeProcessingTiles(queue, () => requestQueueRefresh());

    const gridElement = document.getElementById("photo-grid");
    const show = gridElement?.dataset.show === "from_others" ? "from_others" : "";
    const grid = VaultGrid.find({
        kind: PHOTOS,
        renderTile: renderVaultPhotoTile,
        imageSelector: ".photo-tile img",
        extraParams: show ? { show } : {},
        onSettled: requestQueueRefresh,
    });
    grid?.init();

    const uploader = new VaultUploader(root, grid, {
        kind: PHOTOS,
        field: "image",
        accepts: (file) => file.type.startsWith("image/"),
        refusedMessage: (refused) =>
            refused.length === 1
                ? `${refused[0]!.name} isn't an image. Try the Documents page for files like this.`
                : `${refused.length} files aren't images and were skipped. Try the Documents page for those.`,
        uploadedMessage: (count) => `${count}${count === 1 ? " photo uploaded." : " photos uploaded."} Sorting by location…`,
        lightboxItem: photoLightboxItem,
        // Ingestion is async, so the queue is nudged twice for suggestions to surface without a reload.
        afterUpload: () => {
            refreshQueueAfter(1500);
            refreshQueueAfter(5000);
        },
        companionIds: (id) => [`photo-card-${id}`],
    });
    uploader.bindInputs();
    bindUploadRetry(root, root.dataset.uploadUrl ?? "", "image");
    window.photosDelete = (id) => void uploader.remove(id);
    window.photosOpenLightbox = (id) => uploader.openLightbox(id);
}
