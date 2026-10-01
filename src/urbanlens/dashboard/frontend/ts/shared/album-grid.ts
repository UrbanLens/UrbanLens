/**
 * The Photos tab's album grid: further pages of server-rendered album cards, loaded as the user scrolls.
 */

import { bindPhotoGrid } from "./photo-virtual-grid";

/** One album card from the grid endpoint's server-rendered HTML. */
export function albumCardFromJson(raw: Record<string, unknown>): HTMLElement | null {
    if (typeof raw.html !== "string") return null;
    const template = document.createElement("template");
    template.innerHTML = raw.html.trim();
    const card = template.content.firstElementChild;
    return card instanceof HTMLElement && card.matches(".album-card") ? card : null;
}

export function bindAlbumGrid(grid: HTMLElement): () => void {
    return bindPhotoGrid(grid, {
        inAlbum: false,
        itemSelector: ".album-card[data-album-slug]",
        imageSelector: ".album-card-cover-img",
        renderTile: albumCardFromJson,
        onInserted: (cards) => cards.forEach((card) => window.htmx?.process(card)),
    });
}
