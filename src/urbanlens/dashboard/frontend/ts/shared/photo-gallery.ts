/**
 * The photo gallery card (`partials/pins/_photo_gallery.html`) on the pin, wiki and safety check-in pages: upload
 * by picker or page-wide drop, delete, multi-select, and the hooks its page's map calls through `window.gallery*`.
 *
 * Every handler is delegated and reads the grid when it runs, so an htmx refresh of the card needs no re-binding.
 * The card names its endpoint in `data-gallery-url`, the page kind in `data-gallery-context`, and the pin's bulk
 * endpoint in `data-gallery-bulk-url`.
 */

import type { GalleryMarkerImage } from "../types/globals";
import { getCsrfToken } from "./csrf";
import { confirmAction, toast } from "./dialogs";
import { fetchJson, sendJson } from "./fetch-json";
import { observeProcessingTiles, type ProcessingItem } from "./photo-processing";
import { lightboxItemFromTile, renderPhotoTile, tileFromElement, tileFromJson, tilesForImage, type PhotoTile } from "./photo-tile";

const UPLOAD_TIMEOUT_MS = 10 * 60 * 1000;
const PROGRESS_LINGER_MS = 800;

interface MapUpdate {
    latitude?: string | number | null;
    longitude?: string | number | null;
    map_hidden?: boolean;
}

interface BulkResult {
    deleted?: number;
    unlinked?: number;
}

function card(): HTMLElement | null {
    return document.getElementById("photo-gallery");
}

function grid(): HTMLElement | null {
    return document.getElementById("gallery-grid");
}

function galleryUrl(): string {
    return card()?.dataset.galleryUrl ?? "";
}

function plural(n: number, noun: string): string {
    return `${n} ${noun}${n === 1 ? "" : "s"}`;
}

function gridTiles(): HTMLElement[] {
    return Array.from(grid()?.querySelectorAll<HTMLElement>(".gallery-item[data-id]") ?? []);
}

function gridTile(imgId: number): HTMLElement | null {
    return grid()?.querySelector<HTMLElement>(`.gallery-item[data-id="${imgId}"]`) ?? null;
}

function tileId(el: Element): number {
    return Number.parseInt(el.closest<HTMLElement>(".gallery-item")?.dataset.id ?? "", 10);
}

/** The header badge counts the whole gallery, not the page, so it moves by what the grid gained or lost. */
function adjustCount(delta: number): void {
    const header = card()?.querySelector(".card-header");
    if (!header) return;
    let badge = header.querySelector(".badge");
    const next = Math.max(0, Number.parseInt(badge?.textContent ?? "", 10) || 0) + delta;
    if (next <= 0) {
        badge?.remove();
        return;
    }
    if (!badge) {
        badge = document.createElement("span");
        badge.className = "badge";
        header.querySelector("span")?.after(badge);
    }
    badge.textContent = String(next);
}

function coordsBadge(): HTMLSpanElement {
    const span = document.createElement("span");
    span.className = "gallery-has-coords";
    span.title = "Has GPS coordinates";
    span.innerHTML = `<i class="material-symbols-outlined">place</i>`;
    return span;
}

function markerImage(tile: PhotoTile, raw: ProcessingItem | null): GalleryMarkerImage {
    const markerThumb = raw?.marker_thumb_url;
    return { id: tile.id, url: tile.url, latitude: tile.lat, longitude: tile.lng, ...(typeof markerThumb === "string" && markerThumb ? { marker_thumb_url: markerThumb } : {}) };
}

/** A tile for a photo the viewer just uploaded, matching the server-rendered ones. */
export function buildGalleryTile(tile: PhotoTile, uploaded: boolean): HTMLLIElement {
    const li = renderPhotoTile(tile, { inAlbum: false });
    li.draggable = false;
    li.id = `gallery-item-${tile.id}`;
    li.dataset.uploaded = uploaded ? "true" : "false";
    li.dataset.onPin = "false";
    li.querySelector(".gallery-select-check")?.setAttribute("data-gallery-action", "select");
    const open = li.querySelector<HTMLElement>(".gallery-thumb-btn");
    if (open) {
        open.removeAttribute("data-photo-open");
        open.dataset.galleryAction = "open";
        if (tile.lat != null && tile.lng != null) open.appendChild(coordsBadge());
    }
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "gallery-delete-btn";
    remove.title = "Delete photo";
    remove.dataset.galleryAction = "delete";
    remove.innerHTML = `<i class="material-symbols-outlined">delete</i>`;
    li.appendChild(remove);
    return li;
}

function addToGrid(li: HTMLLIElement): void {
    const target = grid();
    if (!target) return;
    document.getElementById("gallery-empty")?.remove();
    const addTile = document.getElementById("gallery-add-btn");
    if (addTile) {
        target.insertBefore(li, addTile);
    } else {
        target.appendChild(li);
        const add = document.createElement("li");
        add.className = "gallery-add-item";
        add.id = "gallery-add-btn";
        add.innerHTML = `<label for="gallery-file-input" class="gallery-add-label" title="Add more photos"><i class="material-symbols-outlined">add_photo_alternate</i></label>`;
        target.appendChild(add);
    }
    adjustCount(1);
}

function removeTiles(imgId: number): void {
    tilesForImage(imgId).forEach((el) => el.remove());
    window._galleryRemoveMarker?.(imgId);
}

/** A placeholder settles into the finished photo, or leaves the grid when the upload was rejected. */
function settleTile(el: HTMLElement, item: ProcessingItem | null): void {
    const tile = item ? tileFromJson(item) : null;
    if (!tile) {
        el.remove();
        adjustCount(-1);
        return;
    }
    el.replaceWith(buildGalleryTile(tile, item?.uploaded !== false));
    if (!tile.processing && !tile.mapHidden) window._galleryAddMarker?.(markerImage(tile, item));
}

function showProgress(done: number, total: number): void {
    const wrap = document.getElementById("gallery-upload-progress");
    const bar = document.getElementById("gallery-upload-bar");
    if (wrap) wrap.hidden = false;
    if (bar) bar.style.width = `${Math.round((done / total) * 100)}%`;
    if (done < total) return;
    window.setTimeout(() => {
        if (wrap) wrap.hidden = true;
        if (bar) bar.style.width = "0";
    }, PROGRESS_LINGER_MS);
}

async function uploadOne(url: string, file: File): Promise<void> {
    const body = new FormData();
    body.append("image", file);
    const raw = await fetchJson<Record<string, unknown>>(url, {
        method: "POST",
        body,
        headers: { "X-CSRFToken": getCsrfToken() },
        timeoutMs: UPLOAD_TIMEOUT_MS,
        reportsItsOwnErrors: true,
    });
    const tile = raw ? tileFromJson(raw) : null;
    if (!tile) throw new Error("the server sent back no photo");
    addToGrid(buildGalleryTile(tile, raw?.uploaded !== false));
    if (!tile.processing && !tile.mapHidden) window._galleryAddMarker?.(markerImage(tile, raw));
}

export async function uploadFiles(files: FileList | File[]): Promise<void> {
    const list = Array.from(files);
    const url = galleryUrl();
    if (!list.length || !url) return;
    let done = 0;
    showProgress(0, list.length);
    await Promise.all(
        list.map(async (file) => {
            try {
                await uploadOne(url, file);
            } catch (err) {
                toast.error(`Upload failed: ${err instanceof Error && err.message ? err.message : "unknown error"}`);
            } finally {
                showProgress(++done, list.length);
            }
        }),
    );
}

interface DeletePlan {
    withdraw: boolean;
    success: string;
}

/** Ask what deleting this photo should do here; null when the viewer backs out. */
async function planDelete(tile: HTMLElement | null): Promise<DeletePlan | null> {
    const context = card()?.dataset.galleryContext;
    const onWiki = context === "pin" && tile?.dataset.onWiki === "true";
    const keptOnPin = context === "wiki" && tile?.dataset.onPin === "true";
    const uploaded = tile?.dataset.uploaded === "true";
    const ask = (title: string, message: string, confirmLabel: string) => confirmAction({ title, message, confirmLabel });

    if (keptOnPin) {
        if (!(await ask("Remove from the wiki?", "It stays on your pin.", "Remove from wiki"))) return null;
        return { withdraw: false, success: "Removed from the wiki. Still on your pin." };
    }
    if (!onWiki) {
        if (!(await ask("Delete this photo?", "This cannot be undone - the file is removed permanently.", "Delete"))) return null;
        return { withdraw: false, success: "Photo deleted." };
    }
    // A photo fetched from a URL was public before it got here, so there is nothing of the viewer's to withdraw.
    if (!uploaded) {
        const message = "It stays on the community wiki - remove it there if you want it gone from the wiki too.";
        if (!(await ask("Remove from your pin?", message, "Remove"))) return null;
        return { withdraw: false, success: "Removed from this pin. Still on the wiki." };
    }
    const message = "You also contributed it to the community wiki. It stays there unless you say otherwise next.";
    if (!(await ask("Remove from your pin?", message, "Remove"))) return null;
    // Declining keeps the contribution: withdrawing it has to be asked for.
    const withdraw = await confirmAction({
        title: "Also remove it from the community wiki?",
        message: "Removing it from the wiki is permanent.",
        confirmLabel: "Remove from wiki too",
        cancelLabel: "Leave it on the wiki",
    });
    return { withdraw, success: withdraw ? "Photo deleted." : "Removed from this pin. Still on the wiki." };
}

export async function deletePhoto(imgId: number): Promise<void> {
    const plan = await planDelete(gridTile(imgId));
    if (!plan) return;
    try {
        await sendJson(`${galleryUrl()}${imgId}/${plan.withdraw ? "?from_wiki=1" : ""}`, "DELETE", undefined, { reportsItsOwnErrors: true });
    } catch {
        toast.error("Could not delete that photo.");
        return;
    }
    removeTiles(imgId);
    adjustCount(-1);
    toast.success(plan.success);
}

/** Open the lightbox on this grid's photos. `fallback` shows a photo that is not on the rendered page, e.g. from a map marker. */
export function openLightbox(imgId: number, fallback?: { url: string; caption?: string }): void {
    const tiles = gridTiles()
        .map(tileFromElement)
        .filter((tile): tile is PhotoTile => !!tile && !tile.processing);
    const idx = tiles.findIndex((tile) => tile.id === imgId);
    if (idx < 0 && fallback?.url) {
        window.galleryOpenLightboxItem?.([{ url: fallback.url, caption: fallback.caption ?? "", imageId: null }], 0);
        return;
    }
    window.galleryOpenLightboxItem?.(tiles.map(lightboxItemFromTile), Math.max(idx, 0));
}

async function updateOnMap(imgId: number, body: object): Promise<MapUpdate> {
    return (await sendJson<MapUpdate>(`${galleryUrl()}${imgId}/`, "POST", body, { reportsItsOwnErrors: true })) ?? {};
}

export async function repositionPhoto(imgId: number, lat: number, lng: number, onRejected?: () => void): Promise<void> {
    let data: MapUpdate;
    try {
        data = await updateOnMap(imgId, { latitude: lat, longitude: lng });
    } catch (err) {
        toast.error(`Failed to reposition photo: ${err instanceof Error && err.message ? err.message : "unknown error"}`);
        onRejected?.();
        return;
    }
    for (const el of tilesForImage(imgId)) {
        el.dataset.lat = String(data.latitude ?? "");
        el.dataset.lng = String(data.longitude ?? "");
        el.dataset.mapHidden = data.map_hidden ? "true" : "false";
    }
    const open = gridTile(imgId)?.querySelector(".gallery-thumb-btn");
    if (open && !open.querySelector(".gallery-has-coords")) open.appendChild(coordsBadge());
}

export async function setPhotoMapHidden(imgId: number, hidden: boolean, onRejected?: () => void): Promise<void> {
    let data: MapUpdate;
    try {
        data = await updateOnMap(imgId, { map_hidden: hidden });
    } catch (err) {
        toast.error(err instanceof Error && err.message ? err.message : "Could not update map visibility.");
        onRejected?.();
        return;
    }
    const nowHidden = Boolean(data.map_hidden);
    for (const el of tilesForImage(imgId)) el.dataset.mapHidden = nowHidden ? "true" : "false";
    const el = gridTile(imgId) ?? tilesForImage(imgId)[0];
    const tile = el ? tileFromElement(el) : null;
    if (nowHidden) {
        window._galleryRemoveMarker?.(imgId);
        toast.success("Photo hidden from the map. GPS is still saved.");
    } else if (tile && tile.lat != null && tile.lng != null) {
        window._galleryAddMarker?.(markerImage(tile, null));
        toast.success("Photo shown on the map.");
    }
    window._albumSyncMapHidden?.(imgId, nowHidden);
}

// -- Multi-select: the viewer's own photos, deleted or sent to the wiki together. The state lives on the grid.

function selecting(): boolean {
    return card()?.classList.contains("photo-gallery--selecting") ?? false;
}

function selectedIds(): number[] {
    return Array.from(grid()?.querySelectorAll<HTMLElement>(".gallery-item.is-selected") ?? []).map(tileId).filter((id) => id > 0);
}

export function toggleSelectMode(): void {
    const on = !selecting();
    card()?.classList.toggle("photo-gallery--selecting", on);
    document.getElementById("photos-select-btn")?.classList.toggle("is-active", on);
    grid()?.querySelectorAll(".gallery-item.is-selected").forEach((el) => el.classList.remove("is-selected"));
    window.ulBulkToolbar?.clear("photos");
}

function toggleSelected(item: HTMLElement): void {
    item.classList.toggle("is-selected");
    window.ulBulkToolbar?.sync("photos", selectedIds().length, {
        delete: () => void bulkDelete(),
        wiki: () => void bulkSendToWiki(),
        deselect: toggleSelectMode,
    });
}

function bulkUrl(): string {
    return card()?.dataset.galleryBulkUrl ?? "";
}

export async function bulkDelete(): Promise<void> {
    const ids = selectedIds();
    if (!ids.length || !bulkUrl()) return;
    if (!(await confirmAction({ title: `Delete ${plural(ids.length, "photo")}?`, message: "This cannot be undone.", confirmLabel: "Delete" }))) return;
    let result: BulkResult | null;
    try {
        result = await sendJson<BulkResult>(bulkUrl(), "POST", { action: "delete", image_ids: ids }, { reportsItsOwnErrors: true });
    } catch (err) {
        toast.error(err instanceof Error && err.message ? err.message : "Failed to delete photos.");
        return;
    }
    ids.forEach(removeTiles);
    adjustCount(-ids.length);
    const deleted = result?.deleted ?? 0;
    const unlinked = result?.unlinked ?? 0;
    const parts = [deleted ? `Deleted ${plural(deleted, "photo")}.` : "", unlinked ? `Removed ${plural(unlinked, "photo")} from this pin; still on the wiki.` : ""];
    toast.success(parts.filter(Boolean).join(" ") || "Nothing was deleted.");
    toggleSelectMode();
}

export async function bulkSendToWiki(): Promise<void> {
    const ids = selectedIds();
    if (!ids.length || !bulkUrl()) return;
    const message = "This makes them visible to everyone.";
    if (!(await confirmAction({ title: `Send ${plural(ids.length, "photo")} to the community wiki?`, message, confirmLabel: "Send" }))) return;
    try {
        await sendJson(bulkUrl(), "POST", { action: "send_to_wiki", image_ids: ids }, { reportsItsOwnErrors: true });
    } catch (err) {
        toast.error(err instanceof Error && err.message ? err.message : "Failed to send photos to the wiki.");
        return;
    }
    for (const el of ids.flatMap((id) => tilesForImage(id))) el.dataset.onWiki = "true";
    toast.success(`Sent ${plural(ids.length, "photo")} to the wiki.`);
    toggleSelectMode();
}

// -- Page-wide drop to upload

let dragDepth = 0;
let internalDrag = false;

function overlay(): HTMLElement | null {
    return document.getElementById("gallery-drop-overlay");
}

function hideOverlay(): void {
    dragDepth = 0;
    const el = overlay();
    if (el) el.hidden = true;
}

/** In-page drags (a thumbnail, a panel item) carry the image file too, and must never re-upload it. */
function isFilesDrag(event: DragEvent): boolean {
    return !internalDrag && Array.from(event.dataTransfer?.types ?? []).includes("Files");
}

function installDropToUpload(): void {
    document.addEventListener("dragstart", () => (internalDrag = true));
    document.addEventListener("dragend", () => (internalDrag = false));
    document.addEventListener("dragenter", (event) => {
        if (!isFilesDrag(event) || !card()) return;
        dragDepth++;
        const el = overlay();
        if (el) el.hidden = false;
    });
    document.addEventListener("dragleave", (event) => {
        if (!isFilesDrag(event) && dragDepth === 0) return;
        dragDepth = Math.max(0, dragDepth - 1);
        if (dragDepth === 0) hideOverlay();
    });
    document.addEventListener("dragover", (event) => {
        if (event.target instanceof Node && overlay()?.contains(event.target)) event.preventDefault();
    });
    document.addEventListener("drop", (event) => {
        const onOverlay = event.target instanceof Node && !!overlay()?.contains(event.target);
        const wasInternal = internalDrag;
        internalDrag = false;
        hideOverlay();
        if (!onOverlay) return;
        event.preventDefault();
        if (!wasInternal && event.dataTransfer?.files.length) void uploadFiles(event.dataTransfer.files);
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && overlay()?.hidden === false) hideOverlay();
    });
}

// -- Wiring

let observed: { grid: HTMLElement; disconnect: () => void } | null = null;

/**
 * Take up the card's current grid: watch its processing photos, and lift the drop overlay to <body>, since on
 * the pin page the card sits in a sub-tab that hides it whenever another tab is showing.
 */
export function attachGallery(): void {
    const current = grid();
    if (current && observed?.grid !== current) {
        observed?.disconnect();
        observed = { grid: current, disconnect: observeProcessingTiles(current, settleTile) };
    }
    const drop = card()?.parentElement?.querySelector<HTMLElement>(":scope > #gallery-drop-overlay") ?? null;
    if (drop && drop.parentElement !== document.body) {
        document.body.querySelector(":scope > #gallery-drop-overlay")?.remove();
        document.body.appendChild(drop);
    }
}

function onClick(event: MouseEvent): void {
    const control = event.target instanceof Element ? event.target.closest<HTMLElement>("[data-gallery-action]") : null;
    if (!control || !card()?.contains(control)) return;
    const id = tileId(control);
    switch (control.dataset.galleryAction) {
        case "select-mode":
            toggleSelectMode();
            break;
        case "select": {
            event.stopPropagation();
            const item = control.closest<HTMLElement>(".gallery-item");
            if (!item) break;
            if (!selecting()) toggleSelectMode();
            toggleSelected(item);
            break;
        }
        case "open": {
            const item = control.closest<HTMLElement>(".gallery-item");
            if (selecting() && item?.querySelector(".gallery-select-check")) toggleSelected(item);
            else openLightbox(id);
            break;
        }
        case "delete":
            void deletePhoto(id);
            break;
    }
}

function onHover(on: boolean) {
    return (event: MouseEvent): void => {
        const item = event.target instanceof Element ? event.target.closest<HTMLElement>("#gallery-grid .gallery-item[data-id]") : null;
        if (!item || (event.relatedTarget instanceof Node && item.contains(event.relatedTarget))) return;
        window._galleryHighlightMarker?.(tileId(item), on);
    };
}

let installed = false;

export function installPhotoGallery(): void {
    if (installed) return;
    installed = true;
    window.galleryOpenLightbox = openLightbox;
    window.galleryRepositionImage = (imgId, lat, lng, onRejected) => void repositionPhoto(imgId, lat, lng, onRejected);
    window.gallerySetPhotoMapHidden = (imgId, hidden, onRejected) => void setPhotoMapHidden(imgId, hidden, onRejected);
    window.photosToggleSelectMode = toggleSelectMode;

    document.addEventListener("click", onClick);
    document.addEventListener("mouseover", onHover(true));
    document.addEventListener("mouseout", onHover(false));
    document.addEventListener("change", (event) => {
        const input = event.target;
        if (!(input instanceof HTMLInputElement) || input.id !== "gallery-file-input" || !input.files?.length) return;
        void uploadFiles(Array.from(input.files));
        input.value = "";
    });
    installDropToUpload();
    document.addEventListener("htmx:load", attachGallery);
    attachGallery();
}
