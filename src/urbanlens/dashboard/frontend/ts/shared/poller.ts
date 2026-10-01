/**
 * Background polling that stops when nobody can see it.
 */

export interface PollerOptions {
    /** Delay between the end of one tick and the start of the next. */
    intervalMs: number;
    /** The poll ends for good once this element is no longer in the document (e.g. an htmx swap replaced it). */
    element?: Element | null;
    /** Tick once straight away instead of waiting a full interval first. */
    immediate?: boolean;
}

export interface PollerHandle {
    /** End the poll for good and drop its listeners. */
    stop(): void;
    readonly stopped: boolean;
}

/**
 * Run *tick* every `intervalMs` while the page is visible.
 *
 * Ticks never overlap: the next one is scheduled when the previous one settles, and a tick that throws or rejects does
 * not end the poll. While the page is hidden (a background tab, or `pagehide` into the back/forward cache) nothing
 * runs; on return, a tick that fell due in the meantime runs at once and the schedule carries on from there.
 *
 * Args:
 *     tick: The work to repeat.
 *     options: Interval, owning element and whether to tick immediately.
 *
 * Returns:
 *     A handle whose `stop()` ends the poll.
 */
export function startPoller(tick: () => unknown, options: PollerOptions): PollerHandle {
    const { intervalMs, element = null, immediate = false } = options;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let running = false;
    let stopped = false;
    let pageHidden = false;
    let lastStarted = immediate ? -Infinity : Date.now();

    const hidden = (): boolean => pageHidden || document.visibilityState === "hidden";
    const detached = (): boolean => element !== null && !element.isConnected;

    const clearTimer = (): void => {
        if (timer !== null) clearTimeout(timer);
        timer = null;
    };

    const schedule = (): void => {
        clearTimer();
        if (stopped || running || hidden()) return;
        if (detached()) {
            handle.stop();
            return;
        }
        timer = setTimeout(fire, Math.max(0, lastStarted + intervalMs - Date.now()));
    };

    async function fire(): Promise<void> {
        timer = null;
        if (stopped || hidden()) return;
        if (detached()) {
            handle.stop();
            return;
        }
        running = true;
        lastStarted = Date.now();
        try {
            await tick();
        } catch {
            // The next tick is the retry.
        } finally {
            running = false;
            schedule();
        }
    }

    const onVisibility = (): void => {
        if (hidden()) clearTimer();
        else schedule();
    };
    const onPageHide = (): void => {
        pageHidden = true;
        clearTimer();
    };
    const onPageShow = (): void => {
        pageHidden = false;
        schedule();
    };

    const handle: PollerHandle = {
        stop(): void {
            if (stopped) return;
            stopped = true;
            clearTimer();
            document.removeEventListener("visibilitychange", onVisibility);
            window.removeEventListener("pagehide", onPageHide);
            window.removeEventListener("pageshow", onPageShow);
        },
        get stopped(): boolean {
            return stopped;
        },
    };

    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pagehide", onPageHide);
    window.addEventListener("pageshow", onPageShow);
    schedule();
    return handle;
}

export function installGlobalPoller(): void {
    // For inline template scripts, which cannot import.
    window.ulStartPoller = startPoller;
}

declare global {
    interface Window {
        ulStartPoller?: typeof startPoller;
    }
}
