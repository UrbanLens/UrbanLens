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
    /**
     * CSS selector for a tile's `<img>`, used for off-screen pruning. Default:
     * the album grid's. Pass `null` for a grid whose tiles hold no image, which
     * skips the scroll listeners rather than scanning for nothing.
     */
    imageSelector?: string | null;
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
    const imageSelector = opts.imageSelector ?? DEFAULT_IMAGE_SELECTOR;  // only read when `recycles`
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
    // A caller can unbind mid-fetch (changing the sort re-fetches from
    // scratch, which unbinds and rebinds this same grid element) - without
    // this, a fetch already in flight resolves after the sentinel it captured
    // has been removed from the DOM, and `insertBefore(fragment, sentinel)`
    // throws because the reference node is no longer a child of `grid`.
    let unbound = false;
    const abort = new AbortController();

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
            const response = await fetch(url, { signal: abort.signal });
            if (unbound || !response.ok) return;
            const body = (await response.json()) as { items?: Record<string, unknown>[] };
            if (unbound) return;
            const fragment = document.createDocumentFragment();
            for (const raw of body.items ?? []) {
                const el = renderTile(raw);
                if (el) fragment.appendChild(el);
            }
            clearSkeletons();
            grid.insertBefore(fragment, sentinel);
            if (currentLoaded() >= total) sentinel.remove();
        } catch (error) {
            if (!(error instanceof DOMException && error.name === "AbortError")) throw error;
        } finally {
            if (!unbound) clearSkeletons();
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

    // Coalesced into one frame: recycleGridImages reads a bounding rect per
    // loaded tile - a forced synchronous layout each - and scroll fires far
    // more often than the page paints. Once a few thousand tiles are in the
    // DOM the uncoalesced version measured every one of them per event.
    let recycleQueued = false;
    const onScroll = () => {
        if (recycleQueued) return;
        recycleQueued = true;
        requestAnimationFrame(() => {
            recycleQueued = false;
            if (!unbound) recycleGridImages(grid, imageSelector);
        });
    };
    // A grid whose tiles have no <img> (Vault Documents: an icon and a
    // filename) opts out with an explicit `imageSelector: null` and skips the
    // listeners entirely, rather than scanning for a selector that can never
    // match. Omitting the option keeps the default selector - album grids rely
    // on that - so the opt-out has to be the explicit null, not a falsy check.
    const recycles = opts.imageSelector !== null;
    if (recycles) {
        window.addEventListener("scroll", onScroll, { passive: true });
        grid.addEventListener("scroll", onScroll, { passive: true });
    }

    return () => {
        unbound = true;
        abort.abort();
        observer.disconnect();
        if (recycles) {
            window.removeEventListener("scroll", onScroll);
            grid.removeEventListener("scroll", onScroll);
        }
        clearSkeletons();
        sentinel.remove();
    };
}
