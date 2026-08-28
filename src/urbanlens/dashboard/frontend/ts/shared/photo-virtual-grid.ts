/**
 * Windowed album/loose-photo grids: fetch further pages as the user scrolls,
 * and drop decoded image bytes for tiles that have left the viewport plus a
 * buffer so a large album stays usable on a phone.
 */

import { renderPhotoTile, tileFromJson, type PhotoTile } from "./photo-tile";

export const DEFAULT_PAGE_SIZE = 48;
export const UNLOAD_BUFFER_PX = 1200;

export interface VisibleRange {
    start: number;
    end: number;
}

/**
 * The index window that should keep decoded images, given the first/last
 * on-screen tiles and a buffer of extra tiles on each side.
 */
export function bufferedRange(firstVisible: number, lastVisible: number, total: number, buffer: number): VisibleRange {
    return {
        start: Math.max(0, firstVisible - buffer),
        end: Math.min(total, lastVisible + buffer + 1),
    };
}

/** True when a tile's box is far enough off-screen that its bitmap can go. */
export function isFarFromViewport(top: number, bottom: number, viewportHeight: number, bufferPx: number): boolean {
    return bottom < -bufferPx || top > viewportHeight + bufferPx;
}

export function shouldFetchNextPage(loadedCount: number, total: number, sentinelVisible: boolean): boolean {
    return sentinelVisible && loadedCount < total;
}

function recycleGridImages(grid: HTMLElement): void {
    const viewportHeight = window.innerHeight;
    grid.querySelectorAll<HTMLImageElement>(".gallery-thumb").forEach((img) => {
        const rect = img.getBoundingClientRect();
        const far = isFarFromViewport(rect.top, rect.bottom, viewportHeight, UNLOAD_BUFFER_PX);
        if (far) {
            if (img.src) {
                img.dataset.src = img.currentSrc || img.src;
                img.removeAttribute("src");
            }
            return;
        }
        const stored = img.dataset.src;
        if (stored && !img.getAttribute("src")) img.src = stored;
    });
}

interface BindOptions {
    inAlbum: boolean;
    albumSlug?: string;
}

/**
 * Hydrate a grid that already has its first page in the DOM. Further pages
 * load when the trailing sentinel intersects; off-screen bitmaps are dropped.
 */
export function bindPhotoGrid(grid: HTMLElement, opts: BindOptions): () => void {
    const itemsUrl = grid.dataset.itemsUrl;
    const total = Number.parseInt(grid.dataset.photoCount ?? "0", 10);
    const pageSize = Number.parseInt(grid.dataset.gridPageSize ?? "", 10) || DEFAULT_PAGE_SIZE;
    if (!itemsUrl || !total) return () => {};

    let loaded = grid.querySelectorAll(".gallery-item[data-id]").length;
    if (loaded >= total) return () => {};
    let fetching = false;

    const sentinel = document.createElement("li");
    sentinel.className = "photo-grid-sentinel";
    sentinel.setAttribute("aria-hidden", "true");
    grid.appendChild(sentinel);

    const fetchNext = async () => {
        if (fetching || loaded >= total) return;
        fetching = true;
        try {
            const url = `${itemsUrl}${itemsUrl.includes("?") ? "&" : "?"}offset=${loaded}&limit=${pageSize}`;
            const response = await fetch(url);
            if (!response.ok) return;
            const body = (await response.json()) as { items?: Record<string, unknown>[] };
            const tiles: PhotoTile[] = [];
            for (const raw of body.items ?? []) {
                const tile = tileFromJson(raw);
                if (tile) tiles.push(tile);
            }
            const fragment = document.createDocumentFragment();
            for (const tile of tiles) {
                if (opts.inAlbum && opts.albumSlug && !tile.albumSlug) tile.albumSlug = opts.albumSlug;
                fragment.appendChild(renderPhotoTile(tile, { inAlbum: opts.inAlbum, albumSlug: opts.albumSlug }));
            }
            grid.insertBefore(fragment, sentinel);
            loaded += tiles.length;
            if (loaded >= total) sentinel.remove();
        } finally {
            fetching = false;
        }
    };

    const observer = new IntersectionObserver(
        (entries) => {
            if (entries.some((entry) => entry.isIntersecting)) void fetchNext();
        },
        { rootMargin: "800px 0px" },
    );
    observer.observe(sentinel);

    const onScroll = () => recycleGridImages(grid);
    window.addEventListener("scroll", onScroll, { passive: true });
    grid.addEventListener("scroll", onScroll, { passive: true });

    return () => {
        observer.disconnect();
        window.removeEventListener("scroll", onScroll);
        grid.removeEventListener("scroll", onScroll);
        sentinel.remove();
    };
}
