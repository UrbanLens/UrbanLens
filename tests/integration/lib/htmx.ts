/**
 * Waiting for HTMX, which is how most of this application updates itself.
 *
 * Playwright's auto-waiting covers "the element I am about to click exists". It
 * does not cover "the fragment I clicked has come back and been swapped in",
 * which is the step every HTMX interaction in this app depends on. Without an
 * explicit wait, an assertion races the swap and fails roughly one run in ten -
 * the single largest source of flake in a suite like this.
 *
 * The counter is installed as an init script rather than inferred from the
 * `.htmx-request` class, because that class only exists *while* a request is in
 * flight: a check that happens to run between the click and the request
 * starting sees a quiet page and returns immediately.
 */

import type { BrowserContext, Locator, Page } from "@playwright/test";

/** Name of the counter this module installs on `window`. */
const COUNTER = "__ulHtmxPending";

/** How long a page may stay busy before we call it stuck. */
const DEFAULT_SETTLE_TIMEOUT_MS = 15_000;

/** What the injected counter records about a page's HTMX traffic. */
interface HtmxState {
    /** Requests started and not yet finished. */
    pending: number;
    /** Completed swaps, monotonic. */
    settled: number;
    /** Exchanges that failed, monotonic. */
    failed: number;
    /** The most recent failure, for the message when a wait gives up. */
    lastError: string;
}

declare global {
    interface Window {
        [COUNTER]?: HtmxState;
    }
}

/** Reads the counter out of a page, or null when the page has no HTMX. */
async function readState(page: Page): Promise<HtmxState | null> {
    return page.evaluate((counter) => (window as unknown as Record<string, HtmxState | undefined>)[counter] ?? null, COUNTER);
}

/**
 * Installs the in-flight request counter for every document in `target`.
 *
 * Must run before the first navigation, so the `context` fixture calls it at
 * context level - that way a popup or a second page inherits it too. Safe to
 * call twice; the guard inside the script makes re-installation a no-op.
 */
export async function installHtmxTracking(target: Page | BrowserContext): Promise<void> {
    await target.addInitScript(
        ({ counter }) => {
            interface State {
                pending: number;
                settled: number;
                failed: number;
                lastError: string;
            }
            const w = window as unknown as Record<string, State | undefined>;
            if (w[counter]) {
                return;
            }
            const state: State = { pending: 0, settled: 0, failed: 0, lastError: "" };
            w[counter] = state;
            // Listened for on `document` rather than `document.body`: these
            // events bubble, and body does not exist yet at init-script time.
            document.addEventListener("htmx:beforeRequest", () => {
                state.pending += 1;
            });
            const finish = (): void => {
                state.pending = Math.max(0, state.pending - 1);
            };
            // afterRequest fires for success and error alike; sendError and
            // timeout do not always produce one, so they decrement too.
            document.addEventListener("htmx:afterRequest", finish);
            document.addEventListener("htmx:sendError", finish);
            document.addEventListener("htmx:timeout", finish);
            document.addEventListener("htmx:afterSettle", () => {
                state.settled += 1;
            });
            // A non-2xx response is not swapped in, so `afterSettle` never
            // fires for one. Recorded here so a wait for a swap can report
            // "the server answered 500" instead of running out of time and
            // reporting nothing at all.
            const record = (label: string) => (event: Event): void => {
                state.failed += 1;
                const detail = (event as CustomEvent<{ xhr?: XMLHttpRequest; pathInfo?: { requestPath?: string } }>).detail;
                const status = detail?.xhr?.status;
                const path = detail?.pathInfo?.requestPath ?? "";
                state.lastError = `${label}${status ? ` (HTTP ${status})` : ""}${path ? ` for ${path}` : ""}`;
            };
            document.addEventListener("htmx:responseError", record("responseError"));
            document.addEventListener("htmx:sendError", record("sendError"));
            document.addEventListener("htmx:timeout", record("timeout"));
        },
        { counter: COUNTER },
    );
}

/** True when this page loaded HTMX at all. Not every page does. */
export async function hasHtmx(page: Page): Promise<boolean> {
    return page.evaluate(() => "htmx" in window);
}

/**
 * Waits until no HTMX request is in flight.
 *
 * @param timeout Milliseconds to allow. The default is deliberately shorter
 *     than the test timeout so a stuck swap reports as a stuck swap rather than
 *     as the whole test running out of time.
 */
export async function waitForHtmxSettled(page: Page, timeout = DEFAULT_SETTLE_TIMEOUT_MS): Promise<void> {
    await page.waitForFunction(
        (counter) => {
            const state = (window as unknown as Record<string, { pending: number } | undefined>)[counter];
            // No counter means no HTMX on this page; nothing to wait for.
            return state === undefined || state.pending === 0;
        },
        COUNTER,
        { timeout },
    );
}

/**
 * Runs `action` and waits for the HTMX exchange it triggers to complete.
 *
 * Reads the settle count *before* acting, so the wait cannot be satisfied by a
 * swap that had already happened - the mistake that makes a naive
 * "wait for afterSettle" helper pass without waiting for anything.
 *
 * @param page The page the interaction happens on.
 * @param action The interaction, e.g. `() => saveButton.click()`.
 * @param timeout Milliseconds to allow for the swap.
 */
export async function withHtmxSwap(page: Page, action: () => Promise<void>, timeout = DEFAULT_SETTLE_TIMEOUT_MS): Promise<void> {
    const before = await readState(page);
    const previous = before?.settled ?? 0;
    const previousFailures = before?.failed ?? 0;

    await action();

    try {
        await page.waitForFunction(
            ({ counter, settledBefore, failedBefore }) => {
                const state = (window as unknown as Record<string, { pending: number; settled: number; failed: number } | undefined>)[counter];
                if (state === undefined) {
                    return false;
                }
                // Resolve on a failure too, so the branch below can report what
                // went wrong rather than letting the wait expire.
                return state.failed > failedBefore || (state.settled > settledBefore && state.pending === 0);
            },
            { counter: COUNTER, settledBefore: previous, failedBefore: previousFailures },
            { timeout },
        );
    } catch (error) {
        const state = await readState(page);
        if (state === null) {
            throw new Error(`Waited for an HTMX swap on ${page.url()}, but the page has no HTMX loaded at all.`);
        }
        throw new Error(`Waited ${timeout}ms for an HTMX swap on ${page.url()} and none happened (${state.pending} request(s) still in flight).\n${(error as Error).message}`);
    }

    const after = await readState(page);
    if (after && after.failed > previousFailures) {
        throw new Error(`The HTMX exchange failed rather than swapping: ${after.lastError}`);
    }
}

/** Clicks `locator` and waits for the HTMX swap it triggers. */
export async function clickAndSwap(page: Page, locator: Locator, timeout = DEFAULT_SETTLE_TIMEOUT_MS): Promise<void> {
    await withHtmxSwap(page, () => locator.click(), timeout);
}
