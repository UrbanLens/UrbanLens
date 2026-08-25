/**
 * Exercising the Channels/Daphne half of the deployment.
 *
 * WebSockets are served by a different container than the HTTP surface
 * (`app-ws` running Daphne, behind the same proxy), talk to the channel layer
 * in Valkey, and are the one part of the stack a page-load assertion cannot
 * reach. They are also the part most likely to be broken by infrastructure
 * rather than by code - a proxy that does not upgrade, a channel layer that is
 * unreachable, a tunnel that times an idle connection out - which is precisely
 * what an integration run against a real deployment is for.
 *
 * Sockets are opened from inside the page rather than from Node. That keeps the
 * suite free of a WebSocket client dependency, and more importantly means the
 * connection carries the browser's real session cookie and Origin header, so
 * what is tested is what a user's browser would actually do.
 */

import type { Page } from "@playwright/test";

import { env } from "./env.js";

/** What happened when we tried to hold a socket open. */
export interface SocketProbe {
    /** The absolute `wss://` URL that was dialled. */
    url: string;
    /** True if the socket reached `readyState === OPEN`. */
    opened: boolean;
    /** Close code, when the socket closed before the probe finished. */
    closeCode: number | null;
    /** Close reason, when one was given. */
    closeReason: string;
    /** True when the socket was still open at the end of the hold period. */
    stillOpenAtEnd: boolean;
    /** Text frames received, in order. */
    received: string[];
    /** Milliseconds from `new WebSocket(...)` to the `open` event. */
    openLatencyMs: number | null;
}

export interface ProbeOptions {
    /**
     * How long to hold the socket open after it connects, in milliseconds.
     *
     * The default only proves the upgrade succeeded. Raise it past the proxy's
     * idle timeout (Cloudflare's is about 100 seconds) to prove the keep-alive
     * ping actually keeps a quiet connection alive - see `_notification_push.html`.
     */
    holdMs?: number;
    /** Frames to send once open, e.g. the `{"type":"ping"}` keep-alive. */
    send?: unknown[];
    /** Interval for repeating `send`, in milliseconds. 0 sends once. */
    sendEveryMs?: number;
    /** How long to wait for the socket to open at all. */
    connectTimeoutMs?: number;
}

/**
 * Opens `path` as a WebSocket from inside `page` and reports what happened.
 *
 * @param page A page already navigated to the site, so the session cookie is
 *     available to the handshake. An `about:blank` page cannot authenticate.
 * @param path Site-relative socket path, e.g. `/ws/notifications/`.
 */
export async function probeWebSocket(page: Page, path: string, options: ProbeOptions = {}): Promise<SocketProbe> {
    const url = `${env.websocketOrigin}${path.startsWith("/") ? path : `/${path}`}`;
    const holdMs = options.holdMs ?? 1_000;
    const connectTimeoutMs = options.connectTimeoutMs ?? 15_000;

    // No Playwright timeout on the evaluate: the probe bounds itself with its
    // own timers and always resolves with a diagnosis, where an outer timeout
    // would replace that diagnosis with "the call took too long". The spec's
    // own `test.setTimeout` is the backstop for a page that has stopped
    // executing script at all.
    return page.evaluate(
        async ({ url, holdMs, connectTimeoutMs, send, sendEveryMs }) =>
            new Promise<SocketProbe>((resolve) => {
                const startedAt = performance.now();
                const result: SocketProbe = {
                    url,
                    opened: false,
                    closeCode: null,
                    closeReason: "",
                    stillOpenAtEnd: false,
                    received: [],
                    openLatencyMs: null,
                };

                let socket: WebSocket;
                try {
                    socket = new WebSocket(url);
                } catch (error) {
                    result.closeReason = `constructor threw: ${(error as Error).message}`;
                    resolve(result);
                    return;
                }

                let sendTimer: number | undefined;
                let holdTimer: number | undefined;

                const finish = (): void => {
                    window.clearInterval(sendTimer);
                    window.clearTimeout(holdTimer);
                    window.clearTimeout(connectTimer);
                    result.stillOpenAtEnd = socket.readyState === WebSocket.OPEN;
                    try {
                        socket.close(1000, "probe complete");
                    } catch {
                        // Already closing or closed; nothing to report.
                    }
                    resolve(result);
                };

                const connectTimer = window.setTimeout(() => {
                    if (!result.opened) {
                        result.closeReason = `did not open within ${connectTimeoutMs}ms`;
                        finish();
                    }
                }, connectTimeoutMs);

                socket.onopen = () => {
                    result.opened = true;
                    result.openLatencyMs = Math.round(performance.now() - startedAt);
                    window.clearTimeout(connectTimer);

                    const frames = send ?? [];
                    const emit = (): void => {
                        for (const frame of frames) {
                            socket.send(typeof frame === "string" ? frame : JSON.stringify(frame));
                        }
                    };
                    if (frames.length > 0) {
                        emit();
                        if (sendEveryMs && sendEveryMs > 0) {
                            sendTimer = window.setInterval(emit, sendEveryMs);
                        }
                    }

                    holdTimer = window.setTimeout(finish, holdMs);
                };

                socket.onmessage = (event: MessageEvent) => {
                    if (typeof event.data === "string") {
                        result.received.push(event.data);
                    }
                };

                socket.onclose = (event: CloseEvent) => {
                    result.closeCode = event.code;
                    result.closeReason = event.reason;
                    // A close before the hold elapsed is the finding, so report
                    // immediately rather than waiting the rest of the budget out.
                    window.clearInterval(sendTimer);
                    window.clearTimeout(holdTimer);
                    window.clearTimeout(connectTimer);
                    result.stillOpenAtEnd = false;
                    resolve(result);
                };
            }),
        { url, holdMs, connectTimeoutMs, send: options.send, sendEveryMs: options.sendEveryMs ?? 0 },
    );
}

/** A socket the page opened by itself, as seen from outside. */
export interface ObservedSocket {
    url: string;
    closed: boolean;
    frameCount: number;
}

/**
 * Records the sockets `page` opens on its own.
 *
 * Use this to assert the application actually establishes its live connection
 * on a page that should have one, rather than only that a socket *can* be
 * opened by hand.
 */
export function observePageSockets(page: Page): { sockets: ObservedSocket[] } {
    const sockets: ObservedSocket[] = [];
    page.on("websocket", (ws) => {
        const record: ObservedSocket = { url: ws.url(), closed: false, frameCount: 0 };
        sockets.push(record);
        ws.on("framereceived", () => {
            record.frameCount += 1;
        });
        ws.on("framesent", () => {
            record.frameCount += 1;
        });
        ws.on("close", () => {
            record.closed = true;
        });
    });
    return { sockets };
}
