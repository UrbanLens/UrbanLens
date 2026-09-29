/**
 * Debounces saves per key, so edits to one item never delay or drop another's, and saves whatever is still waiting
 * when the page is hidden - a `save` meant to survive that should send with `fetch(..., { keepalive: true })`.
 */
export class KeyedDebounce<T> {
    private readonly waiting = new Map<string, { value: T; timer: ReturnType<typeof setTimeout> }>();
    private readonly onPageHide = (): void => this.flush();

    constructor(
        private readonly delayMs: number,
        private readonly save: (key: string, value: T) => void,
    ) {
        window.addEventListener("pagehide", this.onPageHide);
    }

    schedule(key: string, value: T): void {
        this.cancel(key);
        this.waiting.set(key, { value, timer: setTimeout(() => this.flush(key), this.delayMs) });
    }

    /** Save one key now, or every waiting key when none is given. */
    flush(key?: string): void {
        for (const k of key === undefined ? Array.from(this.waiting.keys()) : [key]) {
            const entry = this.waiting.get(k);
            if (!entry) continue;
            this.cancel(k);
            this.save(k, entry.value);
        }
    }

    cancel(key: string): void {
        const entry = this.waiting.get(key);
        if (entry) clearTimeout(entry.timer);
        this.waiting.delete(key);
    }

    dispose(): void {
        window.removeEventListener("pagehide", this.onPageHide);
        this.waiting.forEach((entry) => clearTimeout(entry.timer));
        this.waiting.clear();
    }
}
