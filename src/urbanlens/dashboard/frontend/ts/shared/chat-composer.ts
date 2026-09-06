/**
 * Keeps a chat composer honest about the difference between "sent" and "accepted".
 *
 * The three game clients cleared their input as soon as `LiveSocket.send()`
 * returned true - which reports transmission, not acceptance. The consumer
 * answers a refused frame with `{"type": "error", "detail": ...}` (an
 * out-of-scope credential, a failed write, and now a volume limit), and none of
 * the clients had a `case "error"` at all, so a refusal arrived as a message
 * that simply vanished from the box. Adding a throttle in front of that would
 * have turned it into routine silent data loss (P31).
 *
 * Ordering is what makes the restore correct rather than approximate. The
 * consumer answers each inbound frame before it reads the next, and a WebSocket
 * preserves order in both directions, so refusals come back in send order: the
 * queue's head is the message an arriving error is about. A confirmation - the
 * sender's own message coming back on the broadcast - retires the head the same
 * way.
 */

import { toast } from "./dialogs";

const DEFAULT_REFUSAL = "Your message couldn't be sent. Please try again.";

/** What the composer needs from a socket, so it can be given a fake in tests. */
export type SendFrame = (payload: { body: string }) => boolean;

/**
 * Report a refusal when there is no composer to give the text back to.
 *
 * A socket whose chat panel was never initialised still receives scope and
 * delivery errors, and dropping those is how this problem started.
 *
 * Args:
 *     detail: The server's explanation, if it sent one.
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
     *
     * Returns:
     *     True when a frame was sent.
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

    /** The sender's own message came back on the broadcast: it was accepted. */
    confirm(body: string): void {
        const index = this.inFlight.indexOf(body);
        if (index !== -1) this.inFlight.splice(index, 1);
    }

    /**
     * An `{"type": "error"}` frame arrived: say so, and give the text back.
     *
     * The input is only refilled when it is empty. Someone who has already
     * started typing the next message should not have it overwritten by the one
     * that bounced - they can see the toast and the text is theirs to retype,
     * which is a smaller loss than clobbering live input.
     *
     * Args:
     *     detail: The server's explanation, if it sent one.
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
