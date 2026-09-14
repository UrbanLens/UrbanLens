/**
 * Keeps a chat composer honest about the difference between "sent" and "accepted".
 */

import { toast } from "./dialogs";

const DEFAULT_REFUSAL = "Your message couldn't be sent. Please try again.";

/** What the composer needs from a socket, so it can be given a fake in tests. */
export type SendFrame = (payload: { body: string }) => boolean;

/**
 * Report a refusal when there is no composer to give the text back to.
 */
export function toastRefusal(detail?: unknown): void {
    toast.error(typeof detail === "string" && detail.trim() ? detail : DEFAULT_REFUSAL);
}

export class ChatComposer {
    /** Bodies sent and not yet confirmed or refused, oldest first. */
    private readonly inFlight: string[] = [];

    constructor(
        private readonly input: HTMLInputElement,
        private readonly send: SendFrame,
    ) {}

    /**
 * Send whatever is in the input, clearing it only if the frame left.
 */
    submit(): boolean {
        const body = this.input.value.trim();
        // send() is false while the socket is reconnecting - leave what they
        // typed in the box rather than clearing it for a message that never left.
        if (!body || !this.send({ body })) return false;
        this.inFlight.push(body);
        this.input.value = "";
        return true;
    }

    /**
 * Forget what is in flight, because the connection that would have answered for it is gone.
 */
    reset(): void {
        this.inFlight.length = 0;
    }

    /** The sender's own message came back on the broadcast: it was accepted. */
    confirm(body: string): void {
        const index = this.inFlight.indexOf(body);
        if (index !== -1) this.inFlight.splice(index, 1);
    }

    /**
 * An `{"type": "error"}` frame arrived: say so, and give the text back.
 */
    reportRefusal(detail?: unknown): void {
        const refused = this.inFlight.shift();
        const message = typeof detail === "string" && detail.trim() ? detail : DEFAULT_REFUSAL;
        if (refused && !this.input.value.trim()) {
            this.input.value = refused;
        }
        toast.error(message);
    }
}
