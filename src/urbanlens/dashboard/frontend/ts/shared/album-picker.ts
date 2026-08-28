/**
 * Add-to-album / move-to-album picker: type-to-filter plus a browsable list.
 */

import { getCsrfToken } from "./csrf";
import { toast } from "./dialogs";

export interface AlbumPickerRow {
    slug: string;
    name: string;
    photo_count: number;
    cover_url: string;
    add_url: string;
}

export function albumMatchesQuery(name: string, query: string): boolean {
    const q = query.trim().toLowerCase();
    return !q || name.toLowerCase().includes(q);
}

interface Pending {
    imageIds: number[];
    moveFrom: string | null;
    onDone?: () => void;
}

let pending: Pending | null = null;

function dialog(): HTMLDialogElement | null {
    return document.getElementById("album-target-dialog") as HTMLDialogElement | null;
}

function applyFilter(query: string): void {
    const dlg = dialog();
    if (!dlg) return;
    const items = dlg.querySelectorAll<HTMLElement>(".album-target-item");
    let visible = 0;
    items.forEach((item) => {
        const match = albumMatchesQuery(item.dataset.name ?? "", query);
        item.hidden = !match;
        if (match) visible += 1;
    });
    const empty = dlg.querySelector<HTMLElement>(".album-target-empty");
    if (empty) empty.hidden = visible > 0 || items.length === 0;
}

async function submitToAlbum(addUrl: string): Promise<void> {
    if (!pending?.imageIds.length) return;
    const body: Record<string, unknown> = { image_ids: pending.imageIds };
    if (pending.moveFrom) body.move_from = pending.moveFrom;
    const response = await fetch(addUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: JSON.stringify(body),
    });
    if (!response.ok) {
        const data = (await response.json().catch(() => ({}))) as { error?: string };
        throw new Error(data.error || response.statusText);
    }
}

export function openAlbumPicker(opts: { imageIds: number[]; moveFrom?: string | null; onDone?: () => void }): void {
    pending = { imageIds: opts.imageIds, moveFrom: opts.moveFrom ?? null, onDone: opts.onDone };
    const dlg = dialog();
    if (!dlg) {
        toast.error("Create an album first.");
        return;
    }
    const title = dlg.querySelector(".album-target-title");
    if (title) title.textContent = pending.moveFrom ? "Move to album" : "Add to album";
    const search = dlg.querySelector<HTMLInputElement>(".album-target-search");
    if (search) search.value = "";
    applyFilter("");
    dlg.showModal();
    search?.focus();
}

document.addEventListener("input", (event) => {
    const search = (event.target as HTMLElement | null)?.closest?.(".album-target-search");
    if (!search) return;
    applyFilter((search as HTMLInputElement).value);
});

document.addEventListener("click", (event) => {
    const btn = (event.target as HTMLElement | null)?.closest?.<HTMLElement>("[data-album-target]");
    if (!btn) return;
    const item = btn.closest<HTMLElement>(".album-target-item");
    const addUrl = item?.dataset.addUrl;
    const dlg = dialog();
    if (!addUrl || !pending || !dlg) return;
    const done = pending.onDone;
    const moving = Boolean(pending.moveFrom);
    const count = pending.imageIds.length;
    void submitToAlbum(addUrl)
        .then(() => {
            dlg.close();
            pending = null;
            toast.success(
                moving
                    ? `Moved ${count} photo${count === 1 ? "" : "s"}.`
                    : `Added ${count} photo${count === 1 ? "" : "s"} to the album.`,
            );
            done?.();
        })
        .catch((err: Error) => toast.error(err.message || "Could not update album."));
});

export function bindAlbumPicker(): void {
    // Listeners are document-delegated; this exists so album-items can call it
    // after a panel swap without installing duplicates.
}
