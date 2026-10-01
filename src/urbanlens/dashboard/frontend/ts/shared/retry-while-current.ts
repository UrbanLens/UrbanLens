/**
 * Retry a step with doubling delays for as long as what it was for is still current.
 */

const FIRST_DELAY_MS = 2000;
const MAX_DELAY_MS = 30000;

export interface RetryOptions {
    /** False once the step no longer matters (the round moved on, the panel closed); retrying stops. */
    isCurrent: () => boolean;
    firstDelayMs?: number;
    maxDelayMs?: number;
    wait?: (ms: number) => Promise<void>;
}

const pause = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Run ``attempt`` until it reports done or ``isCurrent`` turns false, waiting longer after each miss.
 * @param attempt - Resolves true when the step is done, false to try again.
 * @param options - When to stop, and the delays between attempts.
 * @returns True when an attempt succeeded, false when the step stopped being current first.
 */
export async function retryWhileCurrent(attempt: () => Promise<boolean>, options: RetryOptions): Promise<boolean> {
    const wait = options.wait ?? pause;
    const maxDelayMs = options.maxDelayMs ?? MAX_DELAY_MS;
    let delayMs = options.firstDelayMs ?? FIRST_DELAY_MS;
    while (options.isCurrent()) {
        if (await attempt()) {
            return true;
        }
        await wait(delayMs);
        delayMs = Math.min(delayMs * 2, maxDelayMs);
    }
    return false;
}
