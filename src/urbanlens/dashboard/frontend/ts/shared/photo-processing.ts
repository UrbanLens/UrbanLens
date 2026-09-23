/**
 * Placeholder tiles for uploads whose re-encode has not landed, and the poll that swaps the real photo in.
 *
 * A pending upload's stored file is deleted when its re-encode lands, so a tile never requests it: the
 * server lists the photo as `processing` with no file URLs, the tile draws a placeholder, and one batched
 * poll per status URL asks which have settled.
 */

export type ProcessingItem = Record<string, unknown>;

/** Receives the photo's fresh gallery JSON once it has settled (ready, or failed), or null once it is gone. */
export type SettledHandler = (item: ProcessingItem | null) => void;

export interface PollSchedule {
    initialMs: number;
    maxMs: number;
    /** Polls after the last newly watched tile before giving up; the placeholder then stays until a reload. */
    maxPolls: number;
}

/** About ten minutes in all, which outlasts a sandbox queue backed up behind an import. */
export const DEFAULT_SCHEDULE: PollSchedule = { initialMs: 1500, maxMs: 20000, maxPolls: 34 };

/** Matches the server's `_PROCESSING_STATUS_MAX_IDS`. */
export const MAX_IDS_PER_POLL = 100;

export const PROCESSING_LABEL = "Processing…";
export const FAILED_LABEL = "This photo couldn't be processed";

export function pollDelay(poll: number, schedule: PollSchedule = DEFAULT_SCHEDULE): number {
    return Math.min(schedule.initialMs * 2 ** poll, schedule.maxMs);
}

/** `"pending"`, `"failed"`, or `""` for a photo with a file to show. */
export function processingStateOf(raw: ProcessingItem): "pending" | "failed" | "" {
    if (raw.processing === true) return "pending";
    if (raw.processing_failed === true) return "failed";
    return "";
}

/**
 * The thumbnail stand-in: a span, never an `<img>`, so it requests nothing.
 * `baseClass` is the surface's own fallback-tile class.
 */
export function processingPlaceholder(baseClass: string, failed = false): HTMLSpanElement {
    const span = document.createElement("span");
    span.className = failed ? baseClass : `${baseClass} media-processing`;
    span.setAttribute("role", "img");
    span.setAttribute("aria-label", failed ? FAILED_LABEL : PROCESSING_LABEL);
    span.title = failed ? FAILED_LABEL : PROCESSING_LABEL;
    const icon = document.createElement("i");
    icon.className = "material-symbols-outlined";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = failed ? "error" : "hourglass_top";
    span.append(icon);
    return span;
}

interface Watch {
    el: HTMLElement;
    onSettled: SettledHandler;
}

type FetchLike = (url: string, init?: RequestInit) => Promise<Response>;

export class ProcessingPoller {
    private readonly watches = new Map<number, Watch[]>();
    private timer: ReturnType<typeof setTimeout> | null = null;
    private polls = 0;
    private inFlight = false;

    constructor(
        private readonly statusUrl: string,
        private readonly schedule: PollSchedule = DEFAULT_SCHEDULE,
        private readonly fetchImpl: FetchLike = (url, init) => fetch(url, init),
    ) {}

    /** Photos still being waited on. */
    get size(): number {
        return this.watches.size;
    }

    watch(id: number, el: HTMLElement, onSettled: SettledHandler): void {
        const list = this.watches.get(id) ?? [];
        if (list.some((entry) => entry.el === el)) return;
        list.push({ el, onSettled });
        this.watches.set(id, list);
        this.polls = 0;
        this.scheduleNext();
    }

    stop(): void {
        if (this.timer !== null) clearTimeout(this.timer);
        this.timer = null;
        this.watches.clear();
    }

    private pruneDetached(): void {
        for (const [id, list] of this.watches) {
            const live = list.filter((entry) => entry.el.isConnected);
            if (live.length) this.watches.set(id, live);
            else this.watches.delete(id);
        }
    }

    private scheduleNext(): void {
        if (this.timer !== null || this.inFlight) return;
        this.pruneDetached();
        if (!this.watches.size || this.polls >= this.schedule.maxPolls) return;
        this.timer = setTimeout(() => {
            this.timer = null;
            void this.poll();
        }, pollDelay(this.polls, this.schedule));
        this.polls += 1;
    }

    /** One status request for every watched photo; settled ones are handed to their handlers. */
    async poll(): Promise<void> {
        this.pruneDetached();
        const ids = [...this.watches.keys()].slice(0, MAX_IDS_PER_POLL);
        if (!ids.length) return;
        this.inFlight = true;
        try {
            const separator = this.statusUrl.includes("?") ? "&" : "?";
            const response = await this.fetchImpl(`${this.statusUrl}${separator}ids=${ids.join(",")}`, {
                headers: { Accept: "application/json" },
                credentials: "same-origin",
            });
            if (!response.ok) return;
            const body = (await response.json()) as { items?: ProcessingItem[]; processing?: unknown[] };
            const pending = new Set((body.processing ?? []).map(Number));
            const settled = new Map((body.items ?? []).map((item) => [Number(item.id), item] as const));
            for (const id of ids) {
                if (pending.has(id)) continue;
                const list = this.watches.get(id) ?? [];
                this.watches.delete(id);
                const item = settled.get(id) ?? null;
                list.forEach((entry) => entry.onSettled(item));
            }
        } catch {
            // A dropped request is retried by the next poll.
        } finally {
            this.inFlight = false;
            this.scheduleNext();
        }
    }
}

/** On `window`, so every bundle on a page (each carries its own copy of this module) shares one poll per URL. */
function pollerFor(statusUrl: string): ProcessingPoller {
    window.urbanlensProcessingPollers ??= new Map();
    let poller = window.urbanlensProcessingPollers.get(statusUrl);
    if (!poller) {
        poller = new ProcessingPoller(statusUrl);
        window.urbanlensProcessingPollers.set(statusUrl, poller);
    }
    return poller;
}

export type TileSettledHandler = (el: HTMLElement, item: ProcessingItem | null) => void;

/**
 * Watch every pending tile under (or at) *root*. A tile names its photo in `data-id`, and its status URL
 * through the nearest `[data-processing-url]` ancestor.
 */
export function watchProcessingTiles(root: ParentNode, onSettled: TileSettledHandler): void {
    const selector = '[data-processing="pending"][data-id]';
    const tiles = Array.from(root.querySelectorAll<HTMLElement>(selector));
    if (root instanceof HTMLElement && root.matches(selector)) tiles.push(root);
    tiles.forEach((el) => {
        const url = el.closest<HTMLElement>("[data-processing-url]")?.dataset.processingUrl;
        const id = Number(el.dataset.id);
        if (!url || !id) return;
        pollerFor(url).watch(id, el, (item) => onSettled(el, item));
    });
}

/** Watch *container*'s pending tiles now and whenever tiles are added to it. Returns a disconnect. */
export function observeProcessingTiles(container: HTMLElement, onSettled: TileSettledHandler): () => void {
    watchProcessingTiles(container, onSettled);
    const observer = new MutationObserver((mutations) => {
        for (const mutation of mutations) {
            mutation.addedNodes.forEach((node) => {
                if (node instanceof HTMLElement) watchProcessingTiles(node, onSettled);
            });
        }
    });
    observer.observe(container, { childList: true, subtree: true });
    return () => observer.disconnect();
}

/** For the inline scripts in server-rendered galleries (`partials/pins/_photo_gallery.html`). */
export function installGlobalPhotoProcessing(): void {
    window.urbanlensObserveProcessingTiles = observeProcessingTiles;
    window.urbanlensProcessingPlaceholder = processingPlaceholder;
}

declare global {
    interface Window {
        urbanlensProcessingPollers?: Map<string, ProcessingPoller>;
        urbanlensObserveProcessingTiles?: typeof observeProcessingTiles;
        urbanlensProcessingPlaceholder?: typeof processingPlaceholder;
    }
}
