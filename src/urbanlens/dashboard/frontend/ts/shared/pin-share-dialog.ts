/**
 * The pin page's Share dialog (``partials/pins/pin_share_dialog.html``, loaded into ``#pin-share-dialog`` by
 * htmx): pick a friend, optionally rename, attach a map and photos, then review before sending.
 */

import { fetchJson, fetchText } from "./fetch-json";
import { getCsrfToken } from "./csrf";
import { processingPlaceholder, watchAutoProcessingTiles } from "./photo-processing";

interface UploadedPhoto {
    id?: number;
    thumb_url?: string;
    url?: string;
    processing?: boolean;
    processing_failed?: boolean;
}

function byId<T extends HTMLElement>(id: string, type: new () => T): T | null {
    const el = document.getElementById(id);
    return el instanceof type ? el : null;
}

function shareForm(): HTMLFormElement | null {
    return byId("pin-share-form", HTMLFormElement);
}

function filterFriends(query: string): void {
    const q = query.trim().toLowerCase();
    document.querySelectorAll<HTMLElement>("#pin-share-friend-list .pin-share-friend-item").forEach((li) => {
        li.hidden = q.length > 0 && !(li.dataset.search ?? "").includes(q);
    });
}

function pickFriend(btn: HTMLElement): void {
    const input = byId("pin-share-profile-id", HTMLInputElement);
    if (input) input.value = btn.dataset.id ?? "";
    document.querySelectorAll(".pin-share-friend-btn.is-selected").forEach((b) => b.classList.remove("is-selected"));
    btn.classList.add("is-selected");
}

/** Select *tile*'s map, or clear the choice if it was already selected. */
function pickMap(tile: HTMLElement): void {
    const input = byId("pin-share-map-uuid", HTMLInputElement);
    if (!input) return;
    const uuid = tile.dataset.uuid ?? "";
    const wasSelected = input.value === uuid;
    document.querySelectorAll(".pin-share-map-tile.is-selected").forEach((t) => t.classList.remove("is-selected"));
    input.value = wasSelected ? "" : uuid;
    if (!wasSelected) tile.classList.add("is-selected");
}

function newMap(form: HTMLFormElement): void {
    window._openCommentMapComposer?.({
        context: { pinSlug: form.dataset.pinSlug },
        onSaved: (uuid: string) => void refreshMaps(form, uuid),
    });
}

async function refreshMaps(form: HTMLFormElement, selectUuid: string): Promise<void> {
    const grid = document.getElementById("pin-share-map-grid");
    if (!grid) return;
    let html: string;
    try {
        html = await fetchText(form.dataset.mapsUrl ?? "", { reportsItsOwnErrors: true });
    } catch {
        window.toastr?.error("Could not refresh the map list. Close and reopen the dialog.");
        return;
    }
    // The server's own map-grid partial.
    grid.innerHTML = html;
    const tile = Array.from(grid.querySelectorAll<HTMLElement>(".pin-share-map-tile")).find((t) => t.dataset.uuid === selectUuid);
    if (tile) pickMap(tile);
    window._initThumbs?.();
    document.body.dispatchEvent(new CustomEvent("refreshMarkupMaps"));
}

/** The tile for a photo just uploaded here, chosen to be shared. */
export function uploadedPhotoTile(photo: UploadedPhoto, processingUrl: string): HTMLLIElement {
    const li = document.createElement("li");
    li.className = "pin-share-photo";
    li.id = `pin-share-photo-${photo.id ?? ""}`;
    const label = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.name = "image_ids";
    box.value = String(photo.id ?? "");
    box.checked = true;
    label.append(box);
    const src = photo.thumb_url || photo.url;
    if (photo.processing || photo.processing_failed || !src) {
        // The stored file is replaced once re-encoding lands; the page-wide watch swaps the thumbnail in.
        label.append(processingPlaceholder("pin-share-photo-fallback", !!photo.processing_failed));
        if (!photo.processing_failed && photo.id) {
            Object.assign(label.dataset, { processingAuto: "", id: String(photo.id), processing: "pending", processingUrl });
        }
    } else {
        const img = document.createElement("img");
        img.src = src;
        img.alt = "";
        img.loading = "lazy";
        label.append(img);
    }
    const ribbon = document.createElement("span");
    ribbon.className = "pin-share-photo-ribbon";
    ribbon.textContent = "Private";
    const check = document.createElement("span");
    check.className = "pin-share-photo-check";
    const icon = document.createElement("i");
    icon.className = "material-symbols-outlined";
    icon.textContent = "check";
    check.append(icon);
    label.append(ribbon, check);
    li.append(label);
    return li;
}

async function uploadPhotos(form: HTMLFormElement, files: File[]): Promise<void> {
    for (const file of files) {
        const body = new FormData();
        body.append("image", file);
        try {
            const photo = await fetchJson<UploadedPhoto>(form.dataset.uploadUrl ?? "", {
                method: "POST",
                headers: { "X-CSRFToken": getCsrfToken() },
                body,
                reportsItsOwnErrors: true,
            });
            const tile = uploadedPhotoTile(photo ?? {}, form.dataset.processingUrl ?? "");
            document.querySelector(".pin-share-photo-upload")?.before(tile);
            watchAutoProcessingTiles(tile);
        } catch (err) {
            window.toastr?.error(`Upload failed: ${err instanceof Error && err.message ? err.message : "unknown error"}`);
        }
    }
}

function setText(id: string, text: string): void {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}

function showStep(review: boolean): void {
    for (const [id, visible] of [
        ["pin-share-form-fields", !review],
        ["pin-share-form-actions", !review],
        ["pin-share-review", review],
        ["pin-share-review-actions", review],
    ] as const) {
        const el = document.getElementById(id);
        if (el) el.hidden = !visible;
    }
}

/** Show what is about to be shared, or ask for a friend first. */
export function review(): boolean {
    if (!byId("pin-share-profile-id", HTMLInputElement)?.value) {
        window.toastr?.error("Choose a friend to share with.");
        return false;
    }
    const recipient = document.getElementById("pin-share-review-recipient");
    const friend = document.querySelector<HTMLElement>(".pin-share-friend-btn.is-selected");
    if (recipient) {
        recipient.replaceChildren();
        const avatar = friend?.querySelector(".msg-avatar");
        if (friend && avatar) {
            const name = document.createElement("span");
            name.textContent = friend.dataset.username ?? "";
            recipient.append(avatar.cloneNode(true), name);
        }
    }
    const message = byId("pin-share-message", HTMLTextAreaElement)?.value.trim() ?? "";
    const messageEl = document.getElementById("pin-share-review-message");
    if (messageEl) {
        messageEl.textContent = message;
        messageEl.hidden = !message;
    }
    const name = byId("pin-share-custom-name", HTMLInputElement)?.value.trim() ?? "";
    const nameEl = document.getElementById("pin-share-review-name");
    if (nameEl) nameEl.hidden = !name;
    if (name) setText("pin-share-review-name-value", `"${name}"`);
    const photos = document.querySelectorAll('#pin-share-photo-grid input[type="checkbox"]:checked').length;
    const photosEl = document.getElementById("pin-share-review-photos");
    if (photosEl) photosEl.hidden = !photos;
    if (photos) setText("pin-share-review-photo-count", String(photos));
    const mapEl = document.getElementById("pin-share-review-map");
    if (mapEl) mapEl.hidden = !byId("pin-share-map-uuid", HTMLInputElement)?.value;
    showStep(true);
    return true;
}

function onClick(event: MouseEvent): void {
    const target = event.target instanceof Element ? event.target : null;
    const form = shareForm();
    if (!target?.closest("#pin-share-dialog") || !form) return;
    const friend = target.closest<HTMLElement>(".pin-share-friend-btn");
    if (friend) return pickFriend(friend);
    const mapTile = target.closest(".pin-share-map-select-btn")?.closest<HTMLElement>(".pin-share-map-tile");
    if (mapTile) return pickMap(mapTile);
    if (target.closest("#pin-share-new-map-btn")) return newMap(form);
    const action = target.closest<HTMLElement>("[data-pin-share-action]")?.dataset.pinShareAction;
    switch (action) {
        case "use-my-name": {
            const input = byId("pin-share-custom-name", HTMLInputElement);
            if (input) {
                input.value = form.dataset.pinName ?? "";
                input.focus();
            }
            break;
        }
        case "add-attachment": {
            const attachments = document.getElementById("pin-share-attachments");
            if (attachments) attachments.hidden = false;
            const btn = document.getElementById("pin-share-add-attachment-btn");
            if (btn) btn.hidden = true;
            break;
        }
        case "upload":
            byId("pin-share-photo-input", HTMLInputElement)?.click();
            break;
        case "review":
            review();
            break;
        case "back":
            showStep(false);
            break;
    }
}

let installed = false;

export function installPinShareDialog(): void {
    if (installed) return;
    installed = true;
    document.addEventListener("click", onClick);
    document.addEventListener("input", (event) => {
        if (event.target instanceof HTMLInputElement && event.target.matches(".pin-share-friend-filter")) filterFriends(event.target.value);
    });
    document.addEventListener("change", (event) => {
        const input = event.target;
        const form = shareForm();
        if (!(input instanceof HTMLInputElement) || input.id !== "pin-share-photo-input" || !form || !input.files?.length) return;
        const files = Array.from(input.files);
        // Cleared so choosing the same file again still uploads it.
        input.value = "";
        void uploadPhotos(form, files);
    });
}
