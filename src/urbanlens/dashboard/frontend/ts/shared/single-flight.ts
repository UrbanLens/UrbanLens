/**
 * Coalesce overlapping calls to an async task: one run at a time, at most one queued behind it.
 */

const ignore = (): void => {};

/**
 * Wrap *task* so that concurrent calls never run it in parallel.
 *
 * A call while idle starts a run. Every call made while a run is in flight joins one shared trailing run, which starts
 * once the current run settles. A caller therefore always gets the outcome of a run that began after its own call, so
 * an older, slower response can never land after a newer one.
 *
 * The state lives in the returned closure. Frontend bundles each carry their own copy of a shared module, so hand the
 * wrapper itself to other bundles (e.g. on `window`) rather than wrapping the same task twice.
 *
 * Args:
 *     task: The work to serialise. A synchronous throw is reported as a rejection.
 *
 * Returns:
 *     A function that runs or joins *task* and resolves or rejects with the run it joined.
 */
export function singleFlight<T>(task: () => Promise<T>): () => Promise<T> {
    let current: Promise<T> | null = null;
    let trailing: Promise<T> | null = null;

    const start = (): Promise<T> => {
        let run: Promise<T>;
        try {
            run = task();
        } catch (error) {
            run = Promise.reject(error);
        }
        const settled = run.finally(() => {
            if (current === settled) current = null;
        });
        current = settled;
        return settled;
    };

    return () => {
        if (!current) return start();
        trailing ??= current.then(ignore, ignore).then(() => {
            trailing = null;
            return current ?? start();
        });
        return trailing;
    };
}
