/**
 * Saves a value that replaces the whole of what the server holds (an ordering, a document), one request at a time.
 *
 * Since each save supersedes the last, only the newest waiting value is ever sent next, and a failed save that a
 * newer value is already waiting behind is not rolled back: the newer one is sent instead.
 */

export interface LatestWinsCallbacks<T> {
    send: (value: T) => Promise<void>;
    /** Called once the newest requested value is saved. */
    onSaved: (value: T) => void;
    /** Called when the newest requested value failed; `confirmed` is the last value the server accepted. */
    onFailed: (error: unknown, confirmed: T | null) => void;
}

export class LatestWinsSaver<T> {
    private inFlight = false;
    private waiting: { value: T } | null = null;

    constructor(
        private readonly callbacks: LatestWinsCallbacks<T>,
        private confirmed: T | null,
    ) {}

    request(value: T): void {
        if (this.inFlight) {
            this.waiting = { value };
            return;
        }
        void this.run(value);
    }

    private async run(value: T): Promise<void> {
        this.inFlight = true;
        let error: unknown = null;
        let failed = false;
        try {
            await this.callbacks.send(value);
            this.confirmed = value;
        } catch (err) {
            failed = true;
            error = err;
        }
        this.inFlight = false;
        const next = this.waiting;
        this.waiting = null;
        if (next) {
            this.request(next.value);
        } else if (failed) {
            this.callbacks.onFailed(error, this.confirmed);
        } else {
            this.callbacks.onSaved(value);
        }
    }
}
