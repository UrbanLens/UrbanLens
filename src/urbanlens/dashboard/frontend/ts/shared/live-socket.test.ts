import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { openLiveSocket, type LiveSocketHandle } from "./live-socket";

const PING = JSON.stringify({ type: "ping" });
const HEARTBEAT_MS = 45000;

/** What testing/dom-setup.ts registers the document at. */
const PAGE_URL = "https://urbanlens.test/";

/**
 * Put the document back on an https page.
 *
 * The scheme the helper picks comes from ``location``, and the happy-dom
 * document is shared by every test file in the run: ``leave-confirmation.test.ts``
 * navigates it away, so whichever file follows inherits an about:blank location
 * and would see ``ws://`` here for reasons that have nothing to do with this
 * module.
 */
function restorePageUrl(): void {
    (window as unknown as { happyDOM?: { setURL(url: string): void } }).happyDOM?.setURL(PAGE_URL);
}

/** Every socket the helper has constructed this test, oldest first. */
let sockets: StubSocket[] = [];

type Listener = (event: never) => void;

/**
 * A WebSocket the test drives by hand.
 *
 * ``close()`` emits a close event, as a real socket does - which is the whole
 * reason a deliberate close can be mistaken for a dropped one.
 */
class StubSocket {
    static readonly CONNECTING = 0;
    static readonly OPEN = 1;
    static readonly CLOSING = 2;
    static readonly CLOSED = 3;

    readyState: number = StubSocket.CONNECTING;
    readonly sent: string[] = [];
    closedByClient = false;

    private readonly listeners = new Map<string, Listener[]>();

    constructor(readonly url: string) {
        sockets.push(this);
    }

    addEventListener(type: string, fn: Listener): void {
        const existing = this.listeners.get(type);
        if (existing) existing.push(fn);
        else this.listeners.set(type, [fn]);
    }

    removeEventListener(): void {}

    send(data: string): void {
        this.sent.push(data);
    }

    close(): void {
        this.closedByClient = true;
        this.readyState = StubSocket.CLOSED;
        this.emit("close", { code: 1000 });
    }

    /** The server accepted the connection. */
    accept(): void {
        this.readyState = StubSocket.OPEN;
        this.emit("open", {});
    }

    /** The connection went away on its own - a network hiccup, or the tunnel's idle cutoff. */
    drop(code = 1006): void {
        this.readyState = StubSocket.CLOSED;
        this.emit("close", { code });
    }

    deliver(raw: string): void {
        this.emit("message", { data: raw });
    }

    private emit(type: string, event: unknown): void {
        for (const fn of this.listeners.get(type) ?? []) fn(event as never);
    }
}

interface Scheduled {
    id: number;
    due: number;
    fn: () => void;
    /** Null for a setTimeout; the period for a setInterval. */
    everyMs: number | null;
}

/**
 * A clock the test steps by hand.
 *
 * ``bun-types`` does not declare bun's own ``jest.useFakeTimers``, and stubbing
 * the four globals is what the rest of this suite does with ``fetch`` anyway. It
 * also exposes ``pendingTimers()``, which is the only direct way to prove a
 * teardown left nothing armed.
 */
let scheduled = new Map<number, Scheduled>();
let now = 0;
let nextTimerId = 1;

const realTimers = {
    setTimeout: globalThis.setTimeout,
    clearTimeout: globalThis.clearTimeout,
    setInterval: globalThis.setInterval,
    clearInterval: globalThis.clearInterval,
};

function schedule(fn: () => void, ms: number, everyMs: number | null): number {
    const id = nextTimerId++;
    scheduled.set(id, { id, due: now + ms, fn, everyMs });
    return id;
}

function installFakeClock(): void {
    scheduled = new Map();
    now = 0;
    nextTimerId = 1;
    globalThis.setTimeout = ((fn: () => void, ms = 0) => schedule(fn, ms, null)) as unknown as typeof setTimeout;
    globalThis.setInterval = ((fn: () => void, ms = 0) => schedule(fn, ms, ms)) as unknown as typeof setInterval;
    globalThis.clearTimeout = ((id: number) => void scheduled.delete(id)) as unknown as typeof clearTimeout;
    globalThis.clearInterval = ((id: number) => void scheduled.delete(id)) as unknown as typeof clearInterval;
}

function restoreClock(): void {
    Object.assign(globalThis, realTimers);
}

/** Run every callback that comes due in the next *ms*, in the order it would fire. */
function advance(ms: number): void {
    const target = now + ms;
    for (;;) {
        let next: Scheduled | undefined;
        for (const timer of scheduled.values()) {
            if (timer.due <= target && (next === undefined || timer.due < next.due)) next = timer;
        }
        if (next === undefined) break;
        now = next.due;
        if (next.everyMs === null) scheduled.delete(next.id);
        else next.due = now + next.everyMs;
        next.fn();
    }
    now = target;
}

/** Timers still armed. Zero after a teardown is the proof nothing leaked. */
function pendingTimers(): number {
    return scheduled.size;
}

const realWebSocket = globalThis.WebSocket;
const realRandom = Math.random;
const realWarn = console.warn;

/** Pin the jitter so every backoff delay in these tests is exact. */
function stubRandom(value: number): void {
    Math.random = () => value;
}

interface OpenArgs {
    received?: unknown[];
    onMessage?: (data: unknown) => void;
    onOpen?: () => void;
    onPermanentClose?: () => void;
}

/** Every handle opened this test, so afterEach can unsubscribe them all. */
let handles: LiveSocketHandle[] = [];

function open(args: OpenArgs = {}): LiveSocketHandle {
    const handle = openLiveSocket({
        path: "/ws/test/",
        onMessage: args.onMessage ?? ((data) => args.received?.push(data)),
        onOpen: args.onOpen,
        onPermanentClose: args.onPermanentClose,
    });
    handles.push(handle);
    return handle;
}

/** The socket the helper is currently using. */
function current(): StubSocket {
    return sockets[sockets.length - 1]!;
}

beforeEach(() => {
    restorePageUrl();
    sockets = [];
    handles = [];
    globalThis.WebSocket = StubSocket as unknown as typeof WebSocket;
    stubRandom(0);
    installFakeClock();
});

afterEach(() => {
    // window and document are shared across the whole file, so a handle left
    // open here would still be listening for "online" during a later test and
    // would reconnect into its socket list.
    for (const handle of handles) handle.close();
    restoreClock();
    globalThis.WebSocket = realWebSocket;
    Math.random = realRandom;
    console.warn = realWarn;
});

describe("connecting", () => {
    test("uses wss and this page's own host", () => {
        open();
        // The preloaded DOM is registered at https://urbanlens.test/.
        expect(current().url).toBe("wss://urbanlens.test/ws/test/");
    });

    test("opens exactly one socket", () => {
        open();
        expect(sockets.length).toBe(1);
    });

    test("reports open state through the handle", () => {
        const handle = open();
        expect(handle.isOpen()).toBe(false);
        current().accept();
        expect(handle.isOpen()).toBe(true);
    });
});

describe("the heartbeat", () => {
    test("is sent on the interval once the socket is open", () => {
        open();
        current().accept();

        advance(HEARTBEAT_MS);
        expect(current().sent).toEqual([PING]);

        advance(HEARTBEAT_MS);
        expect(current().sent).toEqual([PING, PING]);
    });

    test("gets two pings inside Cloudflare's ~100s idle cutoff", () => {
        // The reason the interval exists: one lost ping must not be enough to
        // let the tunnel time the connection out.
        open();
        current().accept();

        advance(99000);
        expect(current().sent.length).toBeGreaterThanOrEqual(2);
    });

    test("does not start before the socket is open", () => {
        open();
        advance(HEARTBEAT_MS * 3);
        expect(current().sent).toEqual([]);
    });

    test("stops after a deliberate close", () => {
        const handle = open();
        current().accept();
        handle.close();

        advance(HEARTBEAT_MS * 5);
        expect(current().sent).toEqual([]);
    });

    test("does not accumulate one interval per reconnect", () => {
        const handle = open();
        current().accept();
        current().drop();
        advance(1000);
        current().accept();
        current().drop();
        advance(1000);
        current().accept();

        advance(HEARTBEAT_MS);
        // One ping, not three: the intervals from the two dead sockets are gone.
        expect(current().sent).toEqual([PING]);
        handle.close();
    });
});

describe("reconnecting", () => {
    test("happens after an unexpected close", () => {
        open();
        current().accept();
        current().drop();
        expect(sockets.length).toBe(1);

        advance(1000);
        expect(sockets.length).toBe(2);
        expect(current().url).toBe("wss://urbanlens.test/ws/test/");
    });

    test("does not happen after a deliberate close", () => {
        const handle = open();
        current().accept();
        handle.close();

        expect(current().closedByClient).toBe(true);
        advance(60000);
        expect(sockets.length).toBe(1);
    });

    test("backs off further with each failed attempt", () => {
        open();
        current().accept();
        current().drop();

        advance(999);
        expect(sockets.length).toBe(1);
        advance(1);
        expect(sockets.length).toBe(2);

        // Dropped before it ever opened, so the backoff keeps growing.
        current().drop();
        advance(1999);
        expect(sockets.length).toBe(2);
        advance(1);
        expect(sockets.length).toBe(3);

        current().drop();
        advance(3999);
        expect(sockets.length).toBe(3);
        advance(1);
        expect(sockets.length).toBe(4);
    });

    test("starts over from the shortest delay after a connection succeeds", () => {
        open();
        current().accept();
        current().drop();
        advance(1000);

        current().accept();
        current().drop();
        advance(1000);
        expect(sockets.length).toBe(3);
    });

    test("caps the delay rather than growing without bound", () => {
        open();
        current().accept();
        for (let attempt = 0; attempt < 12; attempt += 1) {
            current().drop();
            advance(30000);
        }
        const attempts = sockets.length;

        current().drop();
        advance(30000);
        expect(sockets.length).toBe(attempts + 1);
    });

    test("spreads retries out with jitter", () => {
        stubRandom(1);
        open();
        current().accept();
        current().drop();

        // 1000ms backoff plus the full 25% jitter.
        advance(1000);
        expect(sockets.length).toBe(1);
        advance(250);
        expect(sockets.length).toBe(2);
    });
});

describe("immediate retry triggers", () => {
    test("coming back online retries without waiting out the backoff", () => {
        open();
        current().accept();
        current().drop();

        window.dispatchEvent(new Event("online"));
        expect(sockets.length).toBe(2);
    });

    test("returning to the tab retries without waiting out the backoff", () => {
        open();
        current().accept();
        current().drop();

        document.dispatchEvent(new Event("visibilitychange"));
        expect(sockets.length).toBe(2);
    });

    test("an immediate retry cancels the pending backoff rather than doubling up", () => {
        open();
        current().accept();
        current().drop();

        window.dispatchEvent(new Event("online"));
        advance(60000);
        expect(sockets.length).toBe(2);
    });

    test("they do nothing while the socket is already up", () => {
        open();
        current().accept();

        window.dispatchEvent(new Event("online"));
        document.dispatchEvent(new Event("visibilitychange"));
        expect(sockets.length).toBe(1);
    });
});

describe("teardown", () => {
    test("clears a pending reconnect", () => {
        const handle = open();
        current().accept();
        current().drop();

        handle.close();
        advance(60000);
        expect(sockets.length).toBe(1);
    });

    test("removes the retry listeners", () => {
        const handle = open();
        current().accept();
        handle.close();

        window.dispatchEvent(new Event("online"));
        document.dispatchEvent(new Event("visibilitychange"));
        expect(sockets.length).toBe(1);
    });

    test("leaves no timer armed", () => {
        const handle = open();
        current().accept();
        expect(pendingTimers()).toBe(1); // the heartbeat

        handle.close();
        expect(pendingTimers()).toBe(0);
    });

    test("leaves no timer armed even after several reconnects", () => {
        // The leak this guards against is one heartbeat interval per reconnect,
        // each outliving the socket that started it.
        const handle = open();
        for (let attempt = 0; attempt < 4; attempt += 1) {
            current().accept();
            current().drop();
            advance(30000);
        }
        current().accept();

        handle.close();
        expect(pendingTimers()).toBe(0);
    });

    test("leaves nothing running at all", () => {
        const handle = open();
        current().accept();
        const sent = current().sent.length;
        handle.close();

        advance(600000);
        expect(sockets.length).toBe(1);
        expect(current().sent.length).toBe(sent);
    });
});

describe("incoming frames", () => {
    test("are parsed and handed to the caller", () => {
        const received: unknown[] = [];
        open({ received });
        current().accept();

        current().deliver(JSON.stringify({ type: "chat.message", body: "hi" }));
        expect(received).toEqual([{ type: "chat.message", body: "hi" }]);
    });

    test("an unparseable one does not reach the caller or kill the connection", () => {
        const received: unknown[] = [];
        console.warn = () => {};
        const handle = open({ received });
        current().accept();

        current().deliver("not json");
        expect(received).toEqual([]);
        expect(handle.isOpen()).toBe(true);

        current().deliver(JSON.stringify({ type: "ok" }));
        expect(received).toEqual([{ type: "ok" }]);
    });

    test("an unparseable one is reported rather than swallowed in silence", () => {
        const warnings: unknown[] = [];
        console.warn = (message: unknown) => warnings.push(message);
        open();
        current().accept();

        current().deliver("{");
        expect(warnings.length).toBe(1);
    });

    test("a throw from the caller's handler is not caught", () => {
        // Catching it here would report the caller's own bug as a bad frame.
        open({
            onMessage: () => {
                throw new Error("handler bug");
            },
        });
        current().accept();

        expect(() => current().deliver("{}")).toThrow("handler bug");
    });
});

describe("sending", () => {
    test("JSON-encodes the payload", () => {
        const handle = open();
        current().accept();

        expect(handle.send({ body: "hello" })).toBe(true);
        expect(current().sent).toEqual([JSON.stringify({ body: "hello" })]);
    });

    test("reports failure instead of throwing while the socket is down", () => {
        const handle = open();
        current().accept();
        current().drop();

        expect(handle.send({ body: "hello" })).toBe(false);
    });

    test("reports failure before the socket has opened", () => {
        const handle = open();
        expect(handle.send({ body: "hello" })).toBe(false);
        expect(current().sent).toEqual([]);
    });
});

describe("a refused connection", () => {
    test("stops for good on close 4404 rather than looping", () => {
        // 4404 is the consumers' "not authorized, and retrying will not change
        // that" - a kicked player, a revoked token.
        open();
        current().accept();
        current().drop(4404);

        advance(600000);
        expect(sockets.length).toBe(1);
    });

    test("tells the caller so it can say why", () => {
        let refused = 0;
        open({ onPermanentClose: () => (refused += 1) });
        current().accept();
        current().drop(4404);

        expect(refused).toBe(1);
    });

    test("is not revived by coming back online", () => {
        open();
        current().accept();
        current().drop(4404);

        window.dispatchEvent(new Event("online"));
        document.dispatchEvent(new Event("visibilitychange"));
        expect(sockets.length).toBe(1);
    });
});

describe("onOpen", () => {
    test("fires on every successful connection, not just the first", () => {
        let opened = 0;
        open({ onOpen: () => (opened += 1) });
        current().accept();
        current().drop();
        advance(1000);
        current().accept();

        expect(opened).toBe(2);
    });
});
