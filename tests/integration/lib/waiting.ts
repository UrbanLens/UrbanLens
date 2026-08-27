/**
 * Waiting for work that finishes on its own schedule.
 *
 * The rest of the suite waits for things the browser can be asked about: a
 * navigation, an HTMX swap, an element appearing. This module is for the other
 * kind - a Celery task nobody can observe directly, whose only evidence is that
 * an endpoint starts answering differently some minutes later.
 *
 * Playwright's `expect.poll` covers the simple case and should be preferred
 * where it fits. What it does not give is a *diagnosis* when the wait runs out,
 * and that is the entire difference between a useful failure and a useless one
 * here. "Timed out after 300000ms" says nothing about a pipeline with a dozen
 * stages; "the parcel boundary never arrived - last seen: source=circle,
 * pending=false, refreshing=false, after 41 polls over 5m2s" names the stage
 * that did not run, which is usually the whole answer.
 *
 * Nothing here retries on exception by default. A helper that swallowed errors
 * would turn a 500 into a timeout, and the 500 is the more useful failure.
 */

/** How often to re-check, when a caller does not say. */
const DEFAULT_INTERVAL_MS = 5_000;

/** How long to keep checking, when a caller does not say. */
const DEFAULT_TIMEOUT_MS = 300_000;

export interface WaitOptions<T> {
    /** What is being waited for, as a noun phrase - it appears in the failure. */
    what: string;
    /** Total time to keep trying. Default 5 minutes. */
    timeoutMs?: number;
    /** Gap between attempts. Default 5 seconds. */
    intervalMs?: number;
    /**
     * Renders the most recent value for the failure message.
     *
     * The default JSON-stringifies and truncates, which is right for a small
     * payload and useless for a large one. Pass this whenever the shape being
     * polled is big: the point is to name the two or three fields that explain
     * why the wait ended, not to dump the response.
     */
    describe?: (value: T) => string;
    /**
     * Treat a thrown error as "not ready yet" rather than a failure.
     *
     * Off by default, and worth keeping off: an endpoint that 500s while a
     * test waits for it has already answered the question, and reporting that
     * as a timeout hides it. Turn it on only where an error genuinely is an
     * expected intermediate state - a route that 404s until a row exists.
     */
    tolerateErrors?: boolean;
}

/** Thrown when a wait runs out, carrying what was last seen. */
export class WaitTimeoutError extends Error {
    constructor(
        message: string,
        readonly attempts: number,
        readonly elapsedMs: number,
        readonly lastValue: unknown,
    ) {
        super(message);
        this.name = "WaitTimeoutError";
    }
}

function defaultDescribe(value: unknown): string {
    if (value === undefined) {
        return "nothing (the probe never returned)";
    }
    let rendered: string;
    try {
        rendered = JSON.stringify(value);
    } catch {
        rendered = String(value);
    }
    return rendered.length > 400 ? `${rendered.slice(0, 400)}...` : rendered;
}

function humanDuration(ms: number): string {
    const seconds = Math.round(ms / 1000);
    return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, "0")}s`;
}

/**
 * Polls `probe` until `isReady` accepts its result, and returns that result.
 *
 * @param probe Runs one observation. Called immediately, then on each interval.
 * @param isReady Whether the observation means the wait is over.
 * @param options See {@link WaitOptions}; `what` is required and shapes the failure.
 * @returns The first observation `isReady` accepted.
 * @throws WaitTimeoutError When the timeout elapses, naming the last observation.
 */
export async function waitFor<T>(probe: () => Promise<T>, isReady: (value: T) => boolean, options: WaitOptions<T>): Promise<T> {
    const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    const intervalMs = options.intervalMs ?? DEFAULT_INTERVAL_MS;
    const describe = options.describe ?? defaultDescribe;
    const startedAt = Date.now();

    let attempts = 0;
    let lastValue: T | undefined;
    let lastError: unknown;

    while (Date.now() - startedAt < timeoutMs) {
        attempts += 1;
        try {
            lastValue = await probe();
            lastError = undefined;
            if (isReady(lastValue)) {
                return lastValue;
            }
        } catch (error) {
            if (!options.tolerateErrors) {
                throw error;
            }
            lastError = error;
        }
        const remaining = timeoutMs - (Date.now() - startedAt);
        if (remaining <= 0) {
            break;
        }
        await new Promise((resolve) => setTimeout(resolve, Math.min(intervalMs, remaining)));
    }

    const elapsedMs = Date.now() - startedAt;
    const seen = lastError ? `the last attempt threw: ${(lastError as Error).message}` : `last seen: ${describe(lastValue as T)}`;
    throw new WaitTimeoutError(
        `${options.what} did not happen within ${humanDuration(timeoutMs)} (${attempts} checks over ${humanDuration(elapsedMs)}). ${seen}`,
        attempts,
        elapsedMs,
        lastValue,
    );
}

/**
 * Like {@link waitFor}, but returns null instead of throwing on timeout.
 *
 * For the case where "it never arrived" is a *finding to report* rather than a
 * failure to raise - a spec that wants to assert something specific about the
 * absence, or to attach a diagnosis before failing in its own words.
 *
 * @param probe Runs one observation.
 * @param isReady Whether the observation means the wait is over.
 * @param options See {@link WaitOptions}.
 * @returns The accepted observation, or null if the wait ran out.
 */
export async function waitForOrNull<T>(probe: () => Promise<T>, isReady: (value: T) => boolean, options: WaitOptions<T>): Promise<T | null> {
    try {
        return await waitFor(probe, isReady, options);
    } catch (error) {
        if (error instanceof WaitTimeoutError) {
            return null;
        }
        throw error;
    }
}
