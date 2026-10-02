/**
 * Add-to-album / move-to-album picker. Its rows are server-rendered pages, searched by name, loaded each time it opens.
 */

import { sendForText } from "./fetch-json";
import { toast } from "./dialogs";

interface Pending {
    imageIds: number[];
    moveFrom: string | null;
    onDone?: () => void;
}

let pending: Pending | null = null;

function dialog(): HTMLDialogElement | null {
    return document.getElementById("album-target-dialog") as HTMLDialogElement | null;
}

/** Replace the dialog's rows with a loading row, then its first page. */
export function loadPickerRows(dlg: HTMLElement): void {
    const list = dlg.querySelector<HTMLElement>(".album-target-list");
    const url = dlg.dataset.pickerUrl;
    if (!list || !url) return;
    if (!window.htmx) {
        list.replaceChildren();
        toast.error("Could not load your albums.");
        return;
    }
    list.innerHTML = '<li class="view-loading"><i class="material-icons spin">autorenew</i> Loading albums...</li>';
    void window.htmx.ajax("GET", url, { target: list, swap: "innerHTML" });
}

async function submitToAlbum(addUrl: string): Promise<void> {
    if (!pending?.imageIds.length) return;
    const body: Record<string, unknown> = { image_ids: pending.imageIds };
    if (pending.moveFrom) body.move_from = pending.moveFrom;
    await sendForText(addUrl, "POST", body);
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
    loadPickerRows(dlg);
    dlg.showModal();
    search?.focus();
}

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
