/**
 * Right-click menu on uploaded photos (album grids, pin gallery, wiki gallery).
 */

import { openAlbumPicker } from "./album-picker";
import { getCsrfToken } from "./csrf";
import { toast } from "./dialogs";
import { lightboxItemFromTile, tileFromElement } from "./photo-tile";

const MENU_CLASS = "photo-context-menu";

interface MenuAction {
    icon: string;
    label: string;
    danger?: boolean;
    onClick: () => void;
}

let openMenu: HTMLElement | null = null;

function closeMenu(): void {
    openMenu?.remove();
    openMenu = null;
}

function albumPanel(): HTMLElement | null {
    return document.getElementById("albums-panel");
}

function postJson(url: string, payload: unknown): Promise<Record<string, unknown>> {
    return fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: JSON.stringify(payload),
    }).then(async (response) => {
        const data = (await response.json().catch(() => ({}))) as Record<string, unknown>;
        if (!response.ok) throw new Error(String(data.error || response.statusText));
        return data;
    });
}

function openLightboxFor(el: HTMLElement): void {
    const tile = tileFromElement(el);
    if (!tile || !window.galleryOpenLightboxItem) return;
    const grid = el.closest<HTMLElement>(".gallery-grid");
    if (grid) {
        const items = Array.from(grid.querySelectorAll<HTMLElement>(".gallery-item[data-id]"))
            .map(tileFromElement)
            .filter((item): item is NonNullable<typeof item> => item != null)
            .map(lightboxItemFromTile);
        const idx = items.findIndex((item) => item.imageId === tile.id);
        window.galleryOpenLightboxItem(items, Math.max(idx, 0));
        return;
    }
    window.galleryOpenLightboxItem([lightboxItemFromTile(tile)], 0);
}

function downloadPhoto(url: string): void {
    const a = document.createElement("a");
    a.href = url;
    a.download = "";
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
}

function sharePhoto(imageId: number): void {
    const url = albumPanel()?.dataset.pinShareDialogUrl || document.querySelector<HTMLElement>("[data-pin-share-dialog-url]")?.dataset.pinShareDialogUrl;
    const dlg = document.getElementById("pin-share-dialog") as HTMLDialogElement | null;
    if (!url || !dlg || !window.htmx) {
        toast.info("Open this pin's Share dialog to send photos to a friend.");
        return;
    }
    const onSwap = (event: Event) => {
        if ((event as CustomEvent).detail?.target !== dlg && !(event.target as HTMLElement)?.id?.includes("pin-share")) {
            /* still try */
        }
        dlg.showModal();
        const box = dlg.querySelector<HTMLInputElement>(`#pin-share-photo-${imageId} input[type="checkbox"], input[value="${imageId}"]`);
        if (box) box.checked = true;
        document.body.removeEventListener("htmx:afterSwap", onSwap);
    };
    document.body.addEventListener("htmx:afterSwap", onSwap);
    window.htmx.ajax("GET", url, { target: "#pin-share-dialog", swap: "innerHTML" });
}

function actionsFor(el: HTMLElement): MenuAction[] {
    const tile = tileFromElement(el);
    if (!tile) return [];
    const panel = albumPanel();
    const inAlbum = Boolean(tile.albumSlug || el.closest(".album-item"));
    const bulkUrl = panel?.dataset.galleryBulkUrl || document.getElementById("photo-gallery")?.dataset.galleryBulkUrl || "";
    const shareUrl =
        panel?.dataset.pinShareDialogUrl ||
        document.querySelector<HTMLElement>("[data-pin-share-dialog-url]")?.dataset.pinShareDialogUrl ||
        "";
    const actions: MenuAction[] = [
        { icon: "open_in_full", label: "Open", onClick: () => openLightboxFor(el) },
    ];
    if (tile.url) {
        actions.push({ icon: "download", label: "Download", onClick: () => downloadPhoto(tile.url) });
    }
    if (panel) {
        actions.push({
            icon: "photo_library",
            label: "Add to album",
            onClick: () => openAlbumPicker({ imageIds: [tile.id], onDone: refreshAlbums }),
        });
        if (inAlbum && panel.dataset.editUrl && tile.mine) {
            actions.push({
                icon: "wallpaper",
                label: "Set as album cover",
                onClick: () => setAlbumCover(tile.id),
            });
        }
        if (inAlbum && panel.dataset.removeUrl) {
            actions.push({
                icon: "remove_circle",
                label: "Remove from album",
                onClick: () => {
                    const url = panel.dataset.removeUrl;
                    if (!url) return;
                    el.remove();
                    void postJson(url, { image_ids: [tile.id] }).catch((err: Error) => {
                        toast.error(`Could not remove: ${err.message}`);
                        refreshAlbums();
                    });
                },
            });
        }
    }
    if (bulkUrl && tile.mine && !tile.onWiki) {
        actions.push({
            icon: "public",
            label: "Send to wiki",
            onClick: () => {
                void postJson(bulkUrl, { action: "send_to_wiki", image_ids: [tile.id] })
                    .then(() => toast.success("Sent to the wiki."))
                    .catch((err: Error) => toast.error(err.message || "Could not send to the wiki."));
            },
        });
    }
    if (shareUrl && tile.mine) {
        actions.push({ icon: "person_add", label: "Share with friend", onClick: () => sharePhoto(tile.id) });
    }
    if (tile.mine && bulkUrl) {
        actions.push({
            icon: "delete",
            label: "Delete",
            danger: true,
            onClick: () => {
                if (!window.confirm("Delete this photo? This cannot be undone.")) return;
                void postJson(bulkUrl, { action: "delete", image_ids: [tile.id] })
                    .then(() => {
                        el.remove();
                        toast.success("Photo deleted.");
                    })
                    .catch((err: Error) => toast.error(err.message || "Could not delete."));
            },
        });
    }
    return actions;
}

function refreshAlbums(): void {
    const url = albumPanel()?.dataset.refreshUrl;
    if (url) window.htmx?.ajax("GET", url, { target: "#albums-panel", swap: "outerHTML" });
}

function setAlbumCover(imageId: number): void {
    const url = albumPanel()?.dataset.editUrl;
    if (!url) return;
    void postJson(url, { cover_image_id: imageId })
        .then(() => toast.success("Album cover updated."))
        .catch((err: Error) => toast.error(err.message || "Could not set album cover."));
}

function positionMenu(menu: HTMLElement, clientX: number, clientY: number): void {
    menu.style.left = `${clientX}px`;
    menu.style.top = `${clientY}px`;
    document.body.appendChild(menu);
    const rect = menu.getBoundingClientRect();
    const dx = Math.max(0, rect.right - window.innerWidth + 8);
    const dy = Math.max(0, rect.bottom - window.innerHeight + 8);
    menu.style.left = `${clientX - dx}px`;
    menu.style.top = `${clientY - dy}px`;
}

export function showPhotoContextMenu(el: HTMLElement, clientX: number, clientY: number): HTMLElement | null {
    closeMenu();
    const actions = actionsFor(el);
    if (!actions.length) return null;
    const menu = document.createElement("div");
    menu.className = MENU_CLASS;
    menu.setAttribute("role", "menu");
    for (const action of actions) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = action.danger ? `${MENU_CLASS}__item ${MENU_CLASS}__item--danger` : `${MENU_CLASS}__item`;
        btn.innerHTML = `<i class="material-symbols-outlined">${action.icon}</i><span></span>`;
        const label = btn.querySelector("span");
        if (label) label.textContent = action.label;
        btn.addEventListener("click", () => {
            closeMenu();
            action.onClick();
        });
        menu.appendChild(btn);
    }
    positionMenu(menu, clientX, clientY);
    openMenu = menu;
    return menu;
}

function isPhotoTile(el: HTMLElement | null): el is HTMLElement {
    if (!el?.classList.contains("gallery-item")) return false;
    if (el.classList.contains("album-add-item") || el.classList.contains("gallery-add-item") || el.id === "gallery-empty") {
        return false;
    }
    return Boolean(el.dataset.id);
}

export function bindPhotoContextMenu(): void {
    document.addEventListener("contextmenu", (event) => {
        const tile = (event.target as HTMLElement | null)?.closest<HTMLElement>(".gallery-item") ?? null;
        if (!isPhotoTile(tile)) return;
        event.preventDefault();
        showPhotoContextMenu(tile, event.clientX, event.clientY);
    });
    document.addEventListener("click", (event) => {
        if (openMenu && !openMenu.contains(event.target as Node)) closeMenu();
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") closeMenu();
    });
}
