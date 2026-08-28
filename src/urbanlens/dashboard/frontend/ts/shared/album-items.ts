/**
 * Album panel interactions: browser history, drag-to-reorder, add/remove,
 * uploading into an album, and the photo map toggle.
 *
 * The panel is server-rendered and HTMX-swapped, so everything here
 * re-initialises after each swap rather than binding once on load. Clicks are
 * handled by delegation off `document` for the same reason - the buttons are
 * replaced wholesale on every swap.
 */

import Sortable from "sortablejs";
import { destroyAlbumMap, highlightAlbumPhoto, initAlbumMap } from "./album-map";
import { bindAlbumPicker, openAlbumPicker } from "./album-picker";
import { getCsrfToken } from "./csrf";
import { toast } from "./dialogs";
import { bindPhotoContextMenu } from "./photo-context-menu";
import { lightboxListFromGrid, parsePhotoIds, writePhotoIds } from "./photo-tile";
import { bindPhotoGrid } from "./photo-virtual-grid";

/**
 * How long to wait before re-rendering after the server queues a download.
 * Long enough for a typical provider fetch to land, short enough that the
 * photo doesn't feel lost. A miss is harmless - the next panel render picks
 * it up either way.
 */
const QUEUED_REFRESH_DELAY_MS = 4000;

/** Query parameter that names the open album, and drives Back/Forward. */
const ALBUM_PARAM = "album";

let albumSortable: Sortable | null = null;
/**
 * Set while a swap is being performed *because* of a history navigation, so the
 * resulting render doesn't push the entry we just came from back onto the stack.
 */
let restoringFromHistory = false;

function albumPanel(): HTMLElement | null {
    return document.getElementById("albums-panel");
}

async function postJson(url: string, payload: unknown): Promise<Record<string, unknown>> {
    const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: JSON.stringify(payload),
    });
    if (!response.ok) {
        throw new Error((await response.text()) || response.statusText);
    }
    return (await response.json()) as Record<string, unknown>;
}

/**
 * Re-render the panel in place after a mutation, so counts and the
 * album/loose split stay truthful. Re-fetches whichever view is currently
 * showing (one album, or the album list) from its own URL.
 */
function refreshPanel(): void {
    const panel = albumPanel();
    const url = panel?.dataset.refreshUrl;
    if (!url) return;
    window.htmx?.ajax("GET", url, { target: "#albums-panel", swap: "outerHTML" });
}

// -- Browser history -----------------------------------------------------------
// Opening an album is a navigation as far as the user is concerned, so it gets
// its own history entry. The album is carried in a query parameter rather than
// the hash because the hash already addresses the page's tab (see
// static/js/page-tabs.js), and the tab system preserves the query string when
// it rewrites the hash.

/** The album slug in the current URL, or null on the album list. */
function albumFromUrl(): string | null {
    return new URLSearchParams(window.location.search).get(ALBUM_PARAM);
}

/** Build a URL for the given album (or the list, when null), keeping the tab hash. */
function urlForAlbum(slug: string | null): string {
    const params = new URLSearchParams(window.location.search);
    if (slug) params.set(ALBUM_PARAM, slug);
    else params.delete(ALBUM_PARAM);
    const query = params.toString();
    return `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`;
}

/**
 * Sync the address bar with whichever view the panel is now showing.
 *
 * Pushes a new entry when the album actually changed, so Back returns to the
 * previous view; replaces otherwise, so a plain re-render (after an add, a
 * remove, a reorder) doesn't stack duplicate entries the user has to click
 * through.
 */
function syncHistoryToPanel(): void {
    if (restoringFromHistory) return;
    const shown = albumPanel()?.dataset.albumSlug ?? null;
    if (shown === albumFromUrl()) return;
    window.history.pushState({ album: shown }, "", urlForAlbum(shown));
}

/** Load the view the URL now describes, without pushing another history entry. */
function restoreFromHistory(): void {
    const panel = albumPanel();
    if (!panel) return;

    const wanted = albumFromUrl();
    if (wanted === (panel.dataset.albumSlug ?? null)) return;

    const listUrl = panel.dataset.listUrl;
    if (!listUrl) return;

    restoringFromHistory = true;
    const url = wanted ? `${listUrl}?${ALBUM_PARAM}=${encodeURIComponent(wanted)}` : listUrl;
    window.htmx?.ajax("GET", url, { target: "#albums-panel", swap: "outerHTML" });
}

window.addEventListener("popstate", restoreFromHistory);

// -- Reordering ----------------------------------------------------------------

/** Reorder failed - put the grid back the way the server still has it,
 * rather than leaving a drag shown as if it landed when it didn't. */
function restoreAlbumOrder(grid: HTMLElement, previousOrder: HTMLElement[]): void {
    previousOrder.forEach((el) => grid.appendChild(el));
}

async function saveAlbumOrder(grid: HTMLElement, previousOrder: HTMLElement[]): Promise<void> {
    const panel = albumPanel();
    const url = panel?.dataset.reorderUrl;
    if (!url) return;
    const items = Array.from(grid.querySelectorAll<HTMLElement>(".album-item[data-item-id]")).map((el) =>
        Number.parseInt(el.dataset.itemId ?? "0", 10)
    );
    try {
        await postJson(url, { items });
        toast.success("Photo order saved.");
    } catch (err) {
        toast.error(`Could not save order: ${(err as Error).message}`);
        restoreAlbumOrder(grid, previousOrder);
    }
}

/** Bind Sortable to the album's grid, but only when the album is in custom-order mode. */
export function initAlbumSortable(): void {
    albumSortable?.destroy();
    albumSortable = null;

    const grid = document.getElementById("album-items-grid");
    if (!grid || grid.dataset.albumSortable !== "1") return;

    // Captured on drag start, not derived from oldIndex/newIndex on end: two
    // rapid drags can each fire a save while the earlier one is still in
    // flight, and restoring a snapshot taken *before this specific drag*
    // is what makes a failed save undo only its own change.
    let dragStartOrder: HTMLElement[] = [];
    albumSortable = new Sortable(grid, {
        animation: 150,
        ghostClass: "album-item--ghost",
        fallbackTolerance: 3,
        onStart: () => {
            dragStartOrder = Array.from(grid.querySelectorAll<HTMLElement>(".album-item[data-item-id]"));
        },
        onEnd: () => {
            saveAlbumOrder(grid, dragStartOrder);
        },
    });
}

// -- Uploading into the album --------------------------------------------------

function setUploadProgress(done: number, total: number): void {
    const wrap = document.querySelector<HTMLElement>("[data-album-upload-progress]");
    const bar = document.querySelector<HTMLElement>("[data-album-upload-bar]");
    if (!wrap || !bar) return;
    if (done >= total) {
        window.setTimeout(() => {
            wrap.hidden = true;
            bar.style.width = "0";
        }, 800);
        return;
    }
    wrap.hidden = false;
    bar.style.width = `${Math.round((done / total) * 100)}%`;
}

/**
 * Upload files straight into the open album.
 *
 * Each file is one request, matching the pin/wiki gallery upload contract, so a
 * single rejected file (duplicate, over quota, wrong type) doesn't discard the
 * rest of the batch.
 */
async function uploadFilesToAlbum(files: FileList | File[]): Promise<void> {
    const url = albumPanel()?.dataset.uploadUrl;
    const list = Array.from(files).filter((file) => file.type.startsWith("image/"));
    if (!url || !list.length) return;

    let done = 0;
    let failed = 0;
    setUploadProgress(0, list.length);

    for (const file of list) {
        const body = new FormData();
        body.append("image", file);
        try {
            const response = await fetch(url, { method: "POST", headers: { "X-CSRFToken": getCsrfToken() }, body });
            if (!response.ok) {
                const data = (await response.json().catch(() => ({}))) as { error?: string };
                throw new Error(data.error || `HTTP ${response.status}`);
            }
        } catch (err) {
            failed += 1;
            toast.error(`${file.name}: ${(err as Error).message}`);
        }
        done += 1;
        setUploadProgress(done, list.length);
    }

    const added = list.length - failed;
    if (added > 0) {
        toast.success(`Added ${added} photo${added === 1 ? "" : "s"} to this album.`);
        refreshPanel();
    }
}

// -- Multi-select --------------------------------------------------------------

let selecting = false;
const selected = new Set<number>();

function clearSelect(): void {
    selecting = false;
    selected.clear();
    const panel = albumPanel();
    panel?.classList.remove("albums-panel--selecting");
    document.getElementById("albums-select-btn")?.classList.remove("is-active");
    panel?.querySelectorAll(".gallery-item.is-selected").forEach((el) => el.classList.remove("is-selected"));
    window.ulBulkToolbar?.clear("albums");
    initAlbumSortable();
}

function syncAlbumToolbar(): void {
    const panel = albumPanel();
    const count = selected.size;
    const ids = Array.from(selected);
    const inAlbum = Boolean(panel?.dataset.albumSlug);
    const bulkUrl = panel?.dataset.galleryBulkUrl || "";
    window.ulBulkToolbar?.sync("albums", count, {
        add_to_album: count
            ? () => openAlbumPicker({ imageIds: ids, onDone: () => { clearSelect(); refreshPanel(); } })
            : null,
        move_to_album:
            inAlbum && count
                ? () =>
                      openAlbumPicker({
                          imageIds: ids,
                          moveFrom: panel?.dataset.albumSlug,
                          onDone: () => { clearSelect(); refreshPanel(); },
                      })
                : null,
        remove: inAlbum && count ? () => bulkRemove(ids) : null,
        wiki: bulkUrl && count ? () => bulkWiki(ids, bulkUrl) : null,
        delete: bulkUrl && count ? () => bulkDelete(ids, bulkUrl) : null,
        deselect: () => clearSelect(),
    });
}

function toggleSelecting(): void {
    if (selecting) {
        clearSelect();
        return;
    }
    selecting = true;
    albumPanel()?.classList.add("albums-panel--selecting");
    document.getElementById("albums-select-btn")?.classList.add("is-active");
    albumSortable?.destroy();
    albumSortable = null;
}

function toggleSelected(item: HTMLElement): void {
    const id = Number.parseInt(item.dataset.id ?? "", 10);
    if (!id) return;
    const now = !item.classList.contains("is-selected");
    item.classList.toggle("is-selected", now);
    if (now) selected.add(id);
    else selected.delete(id);
    if (selected.size === 0) {
        // Stay in select mode with an empty bar, matching the pin gallery.
    }
    syncAlbumToolbar();
}

async function bulkRemove(ids: number[]): Promise<void> {
    const url = albumPanel()?.dataset.removeUrl;
    if (!url || !ids.length) return;
    ids.forEach((id) => document.getElementById(`gallery-item-${id}`)?.remove());
    try {
        await postJson(url, { image_ids: ids });
        clearSelect();
    } catch (err) {
        toast.error(`Could not remove: ${(err as Error).message}`);
        refreshPanel();
    }
}

async function bulkWiki(ids: number[], bulkUrl: string): Promise<void> {
    if (!ids.length) return;
    if (!window.confirm(`Send ${ids.length} photo${ids.length === 1 ? "" : "s"} to the community wiki?`)) return;
    try {
        await postJson(bulkUrl, { action: "send_to_wiki", image_ids: ids });
        toast.success(`Sent ${ids.length} photo${ids.length === 1 ? "" : "s"} to the wiki.`);
        clearSelect();
    } catch (err) {
        toast.error((err as Error).message || "Could not send photos to the wiki.");
    }
}

async function bulkDelete(ids: number[], bulkUrl: string): Promise<void> {
    if (!ids.length) return;
    if (!window.confirm(`Delete ${ids.length} photo${ids.length === 1 ? "" : "s"}? This cannot be undone.`)) return;
    try {
        await postJson(bulkUrl, { action: "delete", image_ids: ids });
        ids.forEach((id) => document.getElementById(`gallery-item-${id}`)?.remove());
        toast.success(`Deleted ${ids.length} photo${ids.length === 1 ? "" : "s"}.`);
        clearSelect();
    } catch (err) {
        toast.error((err as Error).message || "Failed to delete photos.");
    }
}

function openAlbumLightbox(tile: HTMLElement): void {
    const grid = tile.closest<HTMLElement>(".gallery-grid");
    if (!grid || !window.galleryOpenLightboxItem) return;
    const { list, idx } = lightboxListFromGrid(grid, tile);
    window.galleryOpenLightboxItem(list, idx);
}

let unbindGrids: Array<() => void> = [];

function initPhotoGrids(): void {
    unbindGrids.forEach((unbind) => unbind());
    unbindGrids = [];
    const panel = albumPanel();
    if (!panel) return;
    const inAlbum = Boolean(panel.dataset.albumSlug);
    panel.querySelectorAll<HTMLElement>("[data-photo-grid]").forEach((grid) => {
        unbindGrids.push(bindPhotoGrid(grid, { inAlbum, albumSlug: panel.dataset.albumSlug }));
    });
}

/**
 * Show the drop overlay only for a real file drag from outside the page.
 *
 * Dragging a thumbnail within the page also carries the image file, so treating
 * every drag as an upload would re-upload photos that are already here. An
 * in-page drag always fires `dragstart` first, which is what distinguishes them
 * (same rule the pin gallery's overlay uses).
 */
let internalDrag = false;
let dragDepth = 0;

function dropOverlay(): HTMLElement | null {
    return document.querySelector<HTMLElement>("[data-album-drop-overlay]");
}

function isFileDrag(event: DragEvent): boolean {
    return !internalDrag && !!event.dataTransfer && Array.from(event.dataTransfer.types || []).includes("Files");
}

function setOverlayVisible(visible: boolean): void {
    const overlay = dropOverlay();
    if (overlay) overlay.hidden = !visible;
}

document.addEventListener("dragstart", (event) => {
    internalDrag = true;
    const tile = (event.target as HTMLElement | null)?.closest?.<HTMLElement>(".gallery-item[data-id]");
    const panel = albumPanel();
    if (!tile || !panel?.contains(tile) || !event.dataTransfer) return;
    const id = Number.parseInt(tile.dataset.id ?? "", 10);
    const ids = selected.has(id) && selected.size ? Array.from(selected) : id ? [id] : [];
    if (ids.length) writePhotoIds(event.dataTransfer, ids);
});
document.addEventListener("dragend", () => {
    internalDrag = false;
});

document.addEventListener("dragenter", (event) => {
    if (!albumPanel()?.dataset.uploadUrl || !isFileDrag(event)) return;
    dragDepth += 1;
    // The pin gallery installs its own document-level drag overlay (see
    // partials/pins/_photo_gallery.html), which uploads to the pin rather than
    // to this album. Both listeners are on document and the gallery's is
    // registered by a later HTMX swap, so its overlay is suppressed on the next
    // frame - by then both handlers have run and ours wins.
    window.requestAnimationFrame(() => {
        if (!dragDepth) return;
        const galleryOverlay = document.getElementById("gallery-drop-overlay");
        if (galleryOverlay) galleryOverlay.hidden = true;
        setOverlayVisible(true);
    });
});

document.addEventListener("dragover", (event) => {
    const card = (event.target as HTMLElement | null)?.closest?.<HTMLElement>("[data-album-drop]");
    if (card && internalDrag) {
        event.preventDefault();
        card.classList.add("album-card--drop");
        return;
    }
    if (!dropOverlay() || dropOverlay()?.hidden) return;
    // Without this the browser navigates to the dropped file instead.
    event.preventDefault();
});

document.addEventListener("dragleave", (event) => {
    const card = (event.target as HTMLElement | null)?.closest?.<HTMLElement>("[data-album-drop]");
    if (card) card.classList.remove("album-card--drop");
    if (dragDepth === 0) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) setOverlayVisible(false);
});

document.addEventListener("drop", (event) => {
    const card = (event.target as HTMLElement | null)?.closest?.<HTMLElement>("[data-album-drop]");
    document.querySelectorAll(".album-card--drop").forEach((el) => el.classList.remove("album-card--drop"));
    if (card && internalDrag) {
        event.preventDefault();
        const ids = parsePhotoIds(event.dataTransfer);
        const addUrl = card.dataset.addUrl;
        internalDrag = false;
        dragDepth = 0;
        setOverlayVisible(false);
        if (addUrl && ids.length) {
            void postJson(addUrl, { image_ids: ids })
                .then(() => {
                    toast.success(`Added ${ids.length} photo${ids.length === 1 ? "" : "s"} to the album.`);
                    clearSelect();
                    refreshPanel();
                })
                .catch((err: Error) => toast.error(`Could not add: ${err.message}`));
        }
        return;
    }
    const overlayShowing = !!dropOverlay() && !dropOverlay()?.hidden;
    dragDepth = 0;
    setOverlayVisible(false);
    if (!overlayShowing || internalDrag) {
        internalDrag = false;
        return;
    }
    event.preventDefault();
    if (event.dataTransfer?.files?.length) uploadFilesToAlbum(event.dataTransfer.files);
});

document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    dragDepth = 0;
    setOverlayVisible(false);
});

// -- Delegated clicks ----------------------------------------------------------

document.addEventListener("change", (event) => {
    const input = event.target as HTMLInputElement;
    if (input?.id !== "album-file-input" || !input.files) return;
    uploadFilesToAlbum(input.files);
    input.value = "";
});

document.addEventListener("click", (event) => {
    const target = event.target as HTMLElement;
    if (!target?.closest) return;

    if (target.closest("[data-album-select-toggle]")) {
        toggleSelecting();
        return;
    }

    const selectCheck = target.closest<HTMLElement>(".gallery-select-check");
    if (selectCheck && albumPanel()?.contains(selectCheck)) {
        event.preventDefault();
        event.stopPropagation();
        const item = selectCheck.closest<HTMLElement>(".gallery-item");
        if (item) {
            if (!selecting) toggleSelecting();
            toggleSelected(item);
        }
        return;
    }

    const photoOpen = target.closest<HTMLElement>("[data-photo-open], .gallery-thumb-btn");
    if (photoOpen && albumPanel()?.contains(photoOpen)) {
        event.preventDefault();
        const tile = photoOpen.closest<HTMLElement>(".gallery-item");
        if (!tile) return;
        if (selecting) {
            toggleSelected(tile);
            return;
        }
        openAlbumLightbox(tile);
        return;
    }

    const createToggle = target.closest<HTMLElement>("[data-album-create-toggle]");
    if (createToggle) {
        const form = document.getElementById("album-create-form");
        if (!form) return;
        const showing = form.hasAttribute("hidden");
        form.toggleAttribute("hidden", !showing);
        createToggle.setAttribute("aria-expanded", String(showing));
        if (showing) form.querySelector<HTMLInputElement>(".album-create-name")?.focus();
        return;
    }

    if (target.closest("[data-album-create-cancel]")) {
        const form = document.getElementById("album-create-form");
        form?.setAttribute("hidden", "");
        document.querySelector<HTMLElement>("[data-album-create-toggle]")?.setAttribute("aria-expanded", "false");
        return;
    }

    // -- Album view: upload / pick / map ---------------------------------------
    if (target.closest("[data-album-upload-btn]")) {
        document.getElementById("album-file-input")?.click();
        return;
    }

    if (target.closest("[data-album-picker-open]")) {
        closeMenus();
        (document.getElementById("album-picker-dialog") as HTMLDialogElement | null)?.showModal();
        return;
    }

    const mapToggle = target.closest<HTMLElement>("[data-album-map-toggle]");
    if (mapToggle) {
        closeMenus();
        const section = document.getElementById("album-map-section");
        if (!section) return;
        const showing = section.hasAttribute("hidden");
        section.toggleAttribute("hidden", !showing);
        document.querySelectorAll<HTMLElement>("[data-album-map-toggle]").forEach((btn) => {
            btn.setAttribute("aria-expanded", String(showing));
        });
        if (showing) initAlbumMap();
        else destroyAlbumMap();
        return;
    }

    // -- Membership ------------------------------------------------------------
    const removeBtn = target.closest<HTMLElement>(".album-item-remove");
    if (removeBtn) {
        event.preventDefault();
        const url = albumPanel()?.dataset.removeUrl;
        const imageId = Number.parseInt(removeBtn.dataset.imageId ?? "0", 10);
        if (!url || !imageId) return;
        const tile = removeBtn.closest<HTMLElement>(".album-item, .gallery-item");
        tile?.remove();
        postJson(url, { image_ids: [imageId] }).catch((err: Error) => {
            toast.error(`Could not remove: ${err.message}`);
            refreshPanel();
        });
        return;
    }

    const addBtn = target.closest<HTMLElement>(".album-item-add");
    if (addBtn) {
        event.preventDefault();
        const url = albumPanel()?.dataset.addUrl;
        const imageId = Number.parseInt(addBtn.dataset.imageId ?? "0", 10);
        if (!url || !imageId) return;
        postJson(url, { image_ids: [imageId] })
            .then(() => {
                toast.success("Added to album.");
                (document.getElementById("album-picker-dialog") as HTMLDialogElement | null)?.close();
                refreshPanel();
            })
            .catch((err: Error) => toast.error(`Could not add: ${err.message}`));
    }
});

/** Close every open album overflow menu (they are <details> elements). */
function closeMenus(): void {
    document.querySelectorAll<HTMLDetailsElement>(".album-menu[open]").forEach((menu) => {
        menu.open = false;
    });
}

// Grid-to-map hover sync, mirroring the map-to-grid direction in album-map.ts.
function syncHoverToMap(event: Event, on: boolean): void {
    const item = (event.target as HTMLElement)?.closest?.<HTMLElement>(".album-item");
    const id = Number.parseInt(item?.dataset.id ?? "0", 10);
    if (id) highlightAlbumPhoto(id, on);
}

document.addEventListener("mouseover", (event) => syncHoverToMap(event, true));
document.addEventListener("mouseout", (event) => syncHoverToMap(event, false));

/**
 * Add an external Media-gallery item to an album.
 *
 * Exposed globally because the Media gallery tiles are rendered by a different
 * (server-rendered, inline-JS) surface than this module owns. Marking the item
 * relevant and caching it locally happens server-side - see
 * services.media.media_relevance.record_relevant_and_cache.
 */
window.albumAddExternalMedia = async (addUrl, media) => {
    toast.info("Saving photo...");
    try {
        const result = await postJson(addUrl, { media });
        if (result.declined) {
            toast.warning((result.message as string) || "You already marked this photo as not relevant.");
            return;
        }
        if (result.error) {
            toast.error(result.error as string);
            return;
        }
        if (result.queued) {
            // The download runs on a worker; give it a moment, then re-render
            // so the photo shows up without the user having to reload.
            toast.success((result.message as string) || "Saving this photo - it'll appear shortly.");
            window.setTimeout(refreshPanel, QUEUED_REFRESH_DELAY_MS);
            return;
        }
        toast.success("Added to album.");
        refreshPanel();
    } catch (err) {
        toast.error(`Could not add: ${(err as Error).message}`);
    }
};

/** Re-bind everything that a panel swap replaced. */
function onPanelRendered(): void {
    // The swap replaced the map's container, so any live map now points at a
    // detached node. Rebuild only if the new render still shows the section.
    destroyAlbumMap();
    if (document.getElementById("album-map-section")?.hasAttribute("hidden") === false) initAlbumMap();

    clearSelect();
    bindAlbumPicker();
    initPhotoGrids();
    initAlbumSortable();
    syncHistoryToPanel();
    restoringFromHistory = false;
}

/**
 * The panel node as of the last render, so an unrelated HTMX swap elsewhere on
 * the page (these pages are full of them) doesn't tear down and rebuild the
 * album map. Comparing node identity is more reliable than reading the swap
 * event's target, which for an outerHTML swap refers to the replaced node.
 */
let lastPanel: HTMLElement | null = null;

document.body.addEventListener("htmx:afterSwap", () => {
    const panel = albumPanel();
    if (panel === lastPanel) return;
    lastPanel = panel;
    if (panel) onPanelRendered();
});

lastPanel = albumPanel();
bindPhotoContextMenu();
initPhotoGrids();
initAlbumSortable();
