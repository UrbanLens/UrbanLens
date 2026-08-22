/**
 * A WebSocket that stays up: it heartbeats through idle timeouts and reconnects
 * after a drop.
 *
 * Every socket in this project reaches the browser through a Cloudflare tunnel,
 * which closes a connection that has carried no traffic for roughly 100 seconds.
 * A lobby waiting for the host to start, or a chat nobody is typing in, is idle
 * by definition, so without a heartbeat those sockets die on their own and the
 * client is left believing it is still connected. The three game clients also
 * had no reconnect at all - their close handler set ``ws = null`` and stopped,
 * so a single drop meant no more live rounds, scores, or chat until a reload.
 *
 * The two inline template clients (``_notification_push.html``,
 * ``_chat_panel.html``) cannot import this - they carry their own copy of the
 * heartbeat interval and point back here for the reasoning.
 */

/**
 * Cloudflare's idle cutoff is ~100s and is not configurable below an Enterprise
 * plan, so the interval has to fit inside it with room to spare: at 45s a ping
 * that gets lost still leaves another one before the cutoff.
 */
const HEARTBEAT_MS = 45000;

/** Precomputed - the frame never varies, and it is sent for the life of the page. */
const HEARTBEAT_FRAME = JSON.stringify({ type: "ping" });

const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 30000;

/**
 * Fraction of the current backoff added at random before each retry. A site that
 * has just come back has every client of it retrying off the same schedule;
 * jitter is what keeps them from arriving in the same instant.
 */
const RECONNECT_JITTER = 0.25;

/**
 * The close code every consumer in ``dashboard/consumers.py`` uses for "not
 * authorized, and retrying will not change that" - a revoked contact token, a
 * player the host kicked, a credential that lost its scope. Reconnecting on it
 * is a busy loop against a refusal, not a recovery, so it stops the socket for
 * good.
 */
const CLOSE_UNAUTHORIZED = 4404;

export interface LiveSocketOptions {
    /** Same-origin path, e.g. ``/ws/notifications/``; the scheme and host are this page's. */
    path: string;
    /** Receives each parsed frame. A throw from here is not caught - see ``onFrame``. */
    onMessage(data: unknown): void;
    onOpen?(): void;
    /** The server refused this connection for good (close 4404); nothing further will arrive. */
    onPermanentClose?(): void;
    /** Override only for a route whose idle timeout differs - the default suits the tunnel. */
    heartbeatMs?: number;
}

export interface LiveSocketHandle {
    /** JSON-encode and send *payload*. False when the socket is not open right now. */
    send(payload: unknown): boolean;
    isOpen(): boolean;
    /** Stop for good: no heartbeat, no reconnect, no listeners left behind. */
    close(): void;
}

/**
 * Open a managed connection to *path* and keep it open.
 *
 * Args:
 *     options: The path to connect to and the callbacks that consume it.
 *
 * Returns:
 *     A handle for sending frames and for shutting the whole thing down.
 */
export function openLiveSocket(options: LiveSocketOptions): LiveSocketHandle {
    const { path, onMessage, onOpen, onPermanentClose, heartbeatMs = HEARTBEAT_MS } = options;

    let socket: WebSocket | null = null;
    let heartbeat: ReturnType<typeof setInterval> | null = null;
    let reconnect: ReturnType<typeof setTimeout> | null = null;
    let backoffMs = RECONNECT_MIN_MS;
    let stopped = false;

    function clearHeartbeat(): void {
        if (heartbeat === null) return;
        clearInterval(heartbeat);
        heartbeat = null;
    }

    function clearReconnect(): void {
        if (reconnect === null) return;
        clearTimeout(reconnect);
        reconnect = null;
    }

    function startHeartbeat(): void {
        // Cleared first because every reconnect passes through here: an interval
        // per reconnect outlives the socket that started it, and a helper that
        // leaks one is worse than the hand-rolled sockets it replaces.
        clearHeartbeat();
        heartbeat = setInterval(() => {
            if (socket?.readyState === WebSocket.OPEN) socket.send(HEARTBEAT_FRAME);
        }, heartbeatMs);
    }

    function scheduleReconnect(): void {
        if (stopped || reconnect !== null) return;
        const delay = backoffMs + Math.random() * backoffMs * RECONNECT_JITTER;
        reconnect = setTimeout(() => {
            reconnect = null;
            connect();
        }, delay);
        backoffMs = Math.min(backoffMs * 2, RECONNECT_MAX_MS);
    }

    function onFrame(event: MessageEvent): void {
        let data: unknown;
        try {
            data = JSON.parse(String(event.data));
        } catch {
            // A frame that cannot be read is not a reason to drop the connection,
            // but it is worth saying so - a socket quietly ignoring everything the
            // server sends looks identical to a socket with nothing to deliver.
            console.warn(`live-socket: ignoring unparseable frame on ${path}`);
            return;
        }
        // Outside the try on purpose: a handler that throws is the caller's bug,
        // and catching it here would bury it in a "bad frame" warning.
        onMessage(data);
    }

    function onClosed(event: CloseEvent): void {
        socket = null;
        clearHeartbeat();
        if (stopped) return;
        if (event.code === CLOSE_UNAUTHORIZED) {
            stopped = true;
            removeRetryTriggers();
            onPermanentClose?.();
            return;
        }
        scheduleReconnect();
    }

    function connect(): void {
        if (stopped || socket !== null) return;
        const proto = location.protocol === "https:" ? "wss://" : "ws://";
        try {
            // Built from this page's own location plus a caller-supplied path -
            // always same-origin.
            socket = new WebSocket(`${proto}${location.host}${path}`); // lgtm[js/request-forgery]
        } catch {
            // Constructing can throw outright (a blocked mixed-content URL, for
            // one), in which case no close event is coming to schedule the retry.
            socket = null;
            scheduleReconnect();
            return;
        }
        socket.addEventListener("open", () => {
            backoffMs = RECONNECT_MIN_MS;
            startHeartbeat();
            onOpen?.();
        });
        socket.addEventListener("message", onFrame);
        socket.addEventListener("close", onClosed);
    }

    /** Coming back online, or back to the tab, beats waiting out the backoff. */
    function retryNow(): void {
        if (stopped || socket !== null) return;
        backoffMs = RECONNECT_MIN_MS;
        clearReconnect();
        connect();
    }

    function onVisibilityChange(): void {
        if (!document.hidden) retryNow();
    }

    function removeRetryTriggers(): void {
        window.removeEventListener("online", retryNow);
        document.removeEventListener("visibilitychange", onVisibilityChange);
    }

    window.addEventListener("online", retryNow);
    document.addEventListener("visibilitychange", onVisibilityChange);
    connect();

    return {
        send(payload: unknown): boolean {
            if (socket?.readyState !== WebSocket.OPEN) return false;
            socket.send(JSON.stringify(payload));
            return true;
        },
        isOpen: () => socket?.readyState === WebSocket.OPEN,
        close(): void {
            // Set before closing so onClosed can tell this from a dropped
            // connection and leave the reconnect alone.
            stopped = true;
            clearHeartbeat();
            clearReconnect();
            removeRetryTriggers();
            socket?.close();
            socket = null;
        },
    };
}
