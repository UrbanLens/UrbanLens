/**
 * How many WebGL maps a page may have alive at once.
 *
 * A browser does not give a page unlimited WebGL contexts - Chrome allows 16 and silently loses
 * the oldest past that, Safari fewer. A comment thread renders one map per comment that has one,
 * so a busy pin's thread asks for more than the browser will hold and the earliest thumbnails go
 * blank with nothing in the console to say why. Each MapLibre instance also carries its own tile
 * cache and worker, which is memory a thumbnail nobody has scrolled to has no reason to hold.
 *
 * So the page keeps a budget: the least recently seen map is disposed when a new one is needed,
 * and its element is marked so whatever built it can build it again if it comes back on screen.
 * Kept here rather than in `comment-map.js` because the interesting behaviour is the eviction
 * order, which is worth testing and is not specific to comments.
 */

/** Well under Chrome's 16, leaving room for a page's own map and an opened dialog's. */
export const MAX_LIVE_THUMB_MAPS = 6;

/** What the budget needs of a live map. `comment-map.js` supplies its `_disposeMapOn`. */
export type DisposeThumbMap = (element: Element) => void;

/**
 * A least-recently-used budget over the maps a page has built.
 *
 * Not a cache: it never builds anything, and it holds no reference to a map - only to the element
 * the map was built into, which is what the caller's disposer takes.
 */
export class ThumbMapBudget {
    /** Insertion-ordered, oldest first; re-inserting moves an entry to the end. */
    private readonly live = new Map<Element, DisposeThumbMap>();

    /**
     * @param limit - How many maps may be alive at once. At most one is evicted per admission, so
     *   lowering this at runtime takes effect gradually rather than in one sweep.
     */
    constructor(private readonly limit: number = MAX_LIVE_THUMB_MAPS) {}

    /** How many maps the page is currently holding. */
    get size(): number {
        return this.live.size;
    }

    /**
     * Record that `element` now holds a live map, evicting the least recently used if that puts
     * the page over budget.
     * @param element - The container the map was built into.
     * @param dispose - Releases that map. Called at most once per admission by this budget.
     * @returns The element evicted to make room, or null when nothing had to go.
     */
    admit(element: Element, dispose: DisposeThumbMap): Element | null {
        // Re-admitting an element that is already live replaces its disposer rather than counting
        // twice - a caller that re-rendered in place would otherwise leak the budget, not the map.
        this.live.delete(element);
        this.live.set(element, dispose);
        if (this.live.size <= this.limit) return null;

        const [oldest] = this.live.keys();
        if (oldest === undefined || oldest === element) return null;
        return this.evict(oldest);
    }

    /** Mark `element` as the most recently used, so it is the last to be evicted. */
    touch(element: Element): void {
        const dispose = this.live.get(element);
        if (dispose === undefined) return;
        this.live.delete(element);
        this.live.set(element, dispose);
    }

    /**
     * Release `element`'s map now, whether or not the page is over budget.
     * @param element - The container to release.
     * @returns The element, or null when it held nothing.
     */
    evict(element: Element): Element | null {
        const dispose = this.live.get(element);
        if (dispose === undefined) return null;
        this.live.delete(element);
        // A disposer that throws must not take the budget's own bookkeeping with it, or the slot
        // stays counted forever and the page ends up holding fewer maps than it is allowed.
        try {
            dispose(element);
        } catch (error) {
            console.error("Failed to dispose a map thumbnail", error);
        }
        return element;
    }

    /** Forget `element` without disposing it - for a map something else already released. */
    forget(element: Element): void {
        this.live.delete(element);
    }

    /** Whether `element` currently holds a map this budget knows about. */
    holds(element: Element): boolean {
        return this.live.has(element);
    }
}

export const thumbMapBudget = new ThumbMapBudget();

/** Publishes the budget on window for the classic inline template scripts and hand-written vanilla JS (e.g. `comment-map.js`). */
export function installGlobalThumbMapBudget(): void {
    window.ThumbMapBudget = thumbMapBudget;
}

declare global {
    interface Window {
        ThumbMapBudget: ThumbMapBudget;
    }
}
