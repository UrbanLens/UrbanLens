/**
 * Windowed photo grids: fetch further pages as the user scrolls, and drop
 * decoded image bytes for tiles that have left the viewport plus a buffer so
 * a large album or the Vault's full library stays usable on a phone.
 *
 * The album grid (default `itemSelector`/`imageSelector`/`renderTile`) and the
 * Vault gallery grid (its own markup - see vault-photo-grid.ts) share this one
 * scroll/fetch/prune engine rather than each growing its own copy.
 */

import { renderPhotoTile, tileFromJson, tileHasImage } from "./photo-tile";

export const DEFAULT_PAGE_SIZE = 48;
export const UNLOAD_BUFFER_PX = 1200;
const DEFAULT_ITEM_SELECTOR = ".gallery-item[data-id]";
const DEFAULT_IMAGE_SELECTOR = ".gallery-thumb";

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

function recycleGridImages(grid: HTMLElement, imageSelector: string): void {
    const viewportHeight = window.innerHeight;
    grid.querySelectorAll<HTMLImageElement>(imageSelector).forEach((img) => {
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
    /** CSS selector for one loaded tile, used to count what's already in the DOM. Default: the album grid's. */
    itemSelector?: string;
    /** CSS selector for a tile's `<img>`, used for off-screen pruning. Default: the album grid's. */
    imageSelector?: string;
    /** Build one tile's element from its raw JSON. Default: the shared album `PhotoTile` renderer. */
    renderTile?: (raw: Record<string, unknown>) => HTMLElement | null;
    /** Extra query params appended to every items-URL fetch (e.g. the active sort). */
    extraParams?: Record<string, string>;
    /** Number of skeleton placeholder tiles shown (then replaced) while a page is in flight. 0 disables. */
    skeletonCount?: number;
    /** Build one skeleton placeholder tile, shown while a page fetch is in flight. Required when skeletonCount > 0. */
    renderSkeleton?: () => HTMLElement;
}

function defaultRenderTile(opts: BindOptions): (raw: Record<string, unknown>) => HTMLElement | null {
    return (raw) => {
        const tile = tileFromJson(raw);
        if (!tile || !tileHasImage(tile)) return null;
        if (opts.inAlbum && opts.albumSlug && !tile.albumSlug) tile.albumSlug = opts.albumSlug;
        return renderPhotoTile(tile, { inAlbum: opts.inAlbum, albumSlug: opts.albumSlug });
    };
}

/**
 * Hydrate a grid that already has its first page in the DOM. Further pages
 * load when the trailing sentinel intersects; off-screen bitmaps are dropped.
 */
export function bindPhotoGrid(grid: HTMLElement, opts: BindOptions): () => void {
    const itemsUrl = grid.dataset.itemsUrl;
    const total = Number.parseInt(grid.dataset.photoCount ?? "0", 10);
    const pageSize = Number.parseInt(grid.dataset.gridPageSize ?? "", 10) || DEFAULT_PAGE_SIZE;
    const itemSelector = opts.itemSelector ?? DEFAULT_ITEM_SELECTOR;
    const imageSelector = opts.imageSelector ?? DEFAULT_IMAGE_SELECTOR;
    const renderTile = opts.renderTile ?? defaultRenderTile(opts);
    if (!itemsUrl || !total) return () => {};

    // Recomputed from the DOM on every fetch, not tracked as running state:
    // a caller can insert/remove tiles of its own between fetches (the Vault
    // gallery's own upload/delete flows do), and a stale counter would then
    // request the wrong offset - skipping some photos or re-fetching ones
    // already on the page as visible duplicates.
    const currentLoaded = () => grid.querySelectorAll(itemSelector).length;
    if (currentLoaded() >= total) return () => {};
    let fetching = false;

    const sentinel = document.createElement("li");
    sentinel.className = "photo-grid-sentinel";
    sentinel.setAttribute("aria-hidden", "true");
    grid.appendChild(sentinel);

    const skeletons: HTMLElement[] = [];
    const showSkeletons = () => {
        if (!opts.skeletonCount || !opts.renderSkeleton) return;
        for (let i = 0; i < opts.skeletonCount; i++) {
            const el = opts.renderSkeleton();
            skeletons.push(el);
            grid.insertBefore(el, sentinel);
        }
    };
    const clearSkeletons = () => {
        skeletons.splice(0).forEach((el) => el.remove());
    };

    const fetchNext = async () => {
        const loaded = currentLoaded();
        if (fetching || loaded >= total) return;
        fetching = true;
        showSkeletons();
        try {
            const params = new URLSearchParams({ offset: String(loaded), limit: String(pageSize), ...(opts.extraParams ?? {}) });
            const url = `${itemsUrl}${itemsUrl.includes("?") ? "&" : "?"}${params.toString()}`;
            const response = await fetch(url);
            if (!response.ok) return;
            const body = (await response.json()) as { items?: Record<string, unknown>[] };
            const fragment = document.createDocumentFragment();
            for (const raw of body.items ?? []) {
                const el = renderTile(raw);
                if (el) fragment.appendChild(el);
            }
            clearSkeletons();
            grid.insertBefore(fragment, sentinel);
            if (currentLoaded() >= total) sentinel.remove();
        } finally {
            clearSkeletons();
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

    const onScroll = () => recycleGridImages(grid, imageSelector);
    window.addEventListener("scroll", onScroll, { passive: true });
    grid.addEventListener("scroll", onScroll, { passive: true });

    return () => {
        observer.disconnect();
        window.removeEventListener("scroll", onScroll);
        grid.removeEventListener("scroll", onScroll);
        clearSkeletons();
        sentinel.remove();
    };
}
