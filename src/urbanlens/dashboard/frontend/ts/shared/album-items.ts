/**
 * Album photo grid interactions: drag-to-reorder, and add/remove buttons.
 *
 * The panel itself is server-rendered and HTMX-swapped, so everything here
 * re-initialises after each swap rather than binding once on load. Clicks are
 * handled by delegation off `document` for the same reason - the buttons are
 * replaced wholesale on every swap.
 */

import Sortable from "sortablejs";
import { getCsrfToken } from "./csrf";
import { toast } from "./dialogs";

/**
 * How long to wait before re-rendering after the server queues a download.
 * Long enough for a typical provider fetch to land, short enough that the
 * photo doesn't feel lost. A miss is harmless - the next panel render picks
 * it up either way.
 */
const QUEUED_REFRESH_DELAY_MS = 4000;

let albumSortable: Sortable | null = null;

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

async function saveAlbumOrder(grid: HTMLElement): Promise<void> {
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
    }
}

/** Bind Sortable to the album's grid, but only when the album is in custom-order mode. */
export function initAlbumSortable(): void {
    albumSortable?.destroy();
    albumSortable = null;

    const grid = document.getElementById("album-items-grid");
    if (!grid || grid.dataset.albumSortable !== "1") return;

    albumSortable = new Sortable(grid, {
        animation: 150,
        ghostClass: "album-item--ghost",
        fallbackTolerance: 3,
        onEnd: () => {
            saveAlbumOrder(grid);
        },
    });
}

document.addEventListener("click", (event) => {
    const target = event.target as HTMLElement;

    const removeBtn = target.closest<HTMLElement>(".album-item-remove");
    if (removeBtn) {
        event.preventDefault();
        const url = albumPanel()?.dataset.removeUrl;
        const imageId = Number.parseInt(removeBtn.dataset.imageId ?? "0", 10);
        if (!url || !imageId) return;
        postJson(url, { image_ids: [imageId] })
            .then(() => {
                toast.success("Removed from album.");
                refreshPanel();
            })
            .catch((err: Error) => toast.error(`Could not remove: ${err.message}`));
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
                refreshPanel();
            })
            .catch((err: Error) => toast.error(`Could not add: ${err.message}`));
    }
});

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

document.body.addEventListener("htmx:afterSwap", () => initAlbumSortable());
initAlbumSortable();
