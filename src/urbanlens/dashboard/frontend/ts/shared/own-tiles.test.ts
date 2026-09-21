/**
 * The pacing and retry policy every path that draws this deployment's own tiles shares.
 *
 * Tested here rather than only through its three callers, because the whole reason it is one module
 * is that a fourth caller should not have to rediscover any of it.
 */
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { acquireOwnTileSlot, fetchOwnTile, isOwnTileUrl, loadOwnTileImage, ownTileRetryDelayMs, resetOwnTileGateForTests } from "./own-tiles";

const realFetch = globalThis.fetch;

/** Answers each call with the next status in `statuses`, repeating the last one thereafter. */
function stubFetch(statuses: Array<number | "network-error">, retryAfterSeconds?: number): { calls: string[] } {
    const state = { calls: [] as string[] };
    globalThis.fetch = ((url: string) => {
        const status = statuses[Math.min(state.calls.length, statuses.length - 1)]!;
        state.calls.push(String(url));
        if (status === "network-error") return Promise.reject(new Error("network error"));
        return Promise.resolve({
            ok: status >= 200 && status < 300,
            status,
            headers: { get: (name: string) => (name.toLowerCase() === "retry-after" && retryAfterSeconds ? String(retryAfterSeconds) : null) },
            arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)),
        } as unknown as Response);
    }) as unknown as typeof fetch;
    return state;
}

beforeEach(() => {
    resetOwnTileGateForTests();
});

afterEach(() => {
    globalThis.fetch = realFetch;
    resetOwnTileGateForTests();
});

/**
 * Own tiles are paced and retried; a vendor's are neither, because a CDN's failure is usually its
 * rate limiter. Everything downstream branches on this one answer.
 */
describe("isOwnTileUrl", () => {
    test("a path is this deployment's own", () => {
        expect(isOwnTileUrl("/dashboard/map/basemap-tiles/street/3/1/2/")).toBe(true);
    });

    test("a vendor's absolute URL is not", () => {
        expect(isOwnTileUrl("https://a.basemaps.cartocdn.com/light_all/3/1/2.png")).toBe(false);
    });

    test("a protocol-relative URL is not, since it names someone else's host", () => {
        expect(isOwnTileUrl("//evil.example/3/1/2.png")).toBe(false);
    });
});

describe("the queue", () => {
    test("hands out its full width at once", async () => {
        const releases = await Promise.all(Array.from({ length: 6 }, () => acquireOwnTileSlot()));

        expect(releases).toHaveLength(6);
        releases.forEach((release) => release());
    });

    test("makes the next caller wait, and hands it the first slot freed", async () => {
        const held = await Promise.all(Array.from({ length: 6 }, () => acquireOwnTileSlot()));
        let granted = false;
        void acquireOwnTileSlot().then(() => {
            granted = true;
        });
        await Promise.resolve();
        expect(granted).toBe(false);

        held[0]!();
        await Promise.resolve();
        await Promise.resolve();

        expect(granted).toBe(true);
        held.slice(1).forEach((release) => release());
    });

    /** A slot handed back twice would widen the queue past the upstream it is there to match. */
    test("releasing twice does not widen it", async () => {
        const release = await acquireOwnTileSlot();
        release();
        release();

        const held = await Promise.all(Array.from({ length: 6 }, () => acquireOwnTileSlot()));
        let granted = false;
        void acquireOwnTileSlot().then(() => {
            granted = true;
        });
        await Promise.resolve();
        await Promise.resolve();

        expect(granted).toBe(false);
        held.forEach((r) => r());
    });
});

describe("the retry schedule", () => {
    test("is bounded, so a dead tile stops asking", () => {
        const delays = [0, 1, 2, 3, 4, 5].map((attempt) => ownTileRetryDelayMs(attempt));

        expect(delays.filter((delay) => delay !== null)).toHaveLength(4);
        expect(delays[4]).toBeNull();
    });

    /**
     * Every tile in a viewport is refused in the same instant. Retrying them all on the same
     * schedule rebuilds exactly the burst that exhausted the slots.
     */
    test("is jittered", () => {
        const drawn = new Set(Array.from({ length: 20 }, () => ownTileRetryDelayMs(0)));

        expect(drawn.size).toBeGreaterThan(1);
    });

    /** An upstream fetch takes ~1.5s, so a retry inside that window is refused for certain. */
    test("never asks again sooner than the Retry-After the proxy sends", () => {
        for (let i = 0; i < 50; i++) expect(ownTileRetryDelayMs(0)!).toBeGreaterThanOrEqual(500);
    });

    /** The proxy knows how long its own slots are held for; this schedule is only guessing. */
    test("waits as long as the server asked when the server asked for longer", () => {
        for (let attempt = 0; attempt < 4; attempt++) {
            expect(ownTileRetryDelayMs(attempt, 30_000)).toBeGreaterThanOrEqual(30_000);
        }
    });

    /** The schedule is also spreading a viewport's tiles apart from each other, which a short ask should not undo. */
    test("does not shorten to a server that asked for less than the schedule", () => {
        for (let i = 0; i < 20; i++) expect(ownTileRetryDelayMs(3, 100)!).toBeGreaterThan(1000);
    });

    /**
     * The jitter has to survive the server's ask, not be clamped away by it. Every refused tile
     * carries the proxy's own `Retry-After: 1`, which is the same 1000ms as the first scheduled
     * delay - so a schedule that only jitters downward from 1000 has half its draws raised back to
     * exactly 1000, and half of every refused cohort retries in the same millisecond.
     */
    test("stays jittered when the server's ask matches the schedule", () => {
        const drawn = Array.from({ length: 200 }, () => ownTileRetryDelayMs(0, 1000)!);

        expect(new Set(drawn).size).toBeGreaterThan(100);
        expect(drawn.filter((delay) => delay === 1000)).toHaveLength(0);
    });

    test("a server asking for a wait does not buy the tile another attempt", () => {
        expect(ownTileRetryDelayMs(4, 30_000)).toBeNull();
    });
});

describe("fetchOwnTile", () => {
    const realSetTimeout = globalThis.setTimeout;
    let scheduled: Array<{ fn: () => void; ms: number }>;

    beforeEach(() => {
        scheduled = [];
        globalThis.setTimeout = ((fn: () => void, ms: number) => {
            scheduled.push({ fn, ms });
            return 0;
        }) as unknown as typeof setTimeout;
    });

    afterEach(() => {
        globalThis.setTimeout = realSetTimeout;
    });

    /**
     * Runs the backoff timers without waiting out the real schedule, leaving the much longer slot
     * watchdog alone - a released slot has to come from the request finishing, not from time
     * passing, or the tests below would stop saying anything about whether it does.
     */
    async function advanceRetries(): Promise<void> {
        for (let round = 0; round < 10; round++) {
            const due = scheduled.filter((timer) => timer.ms < 20_000);
            scheduled = scheduled.filter((timer) => timer.ms >= 20_000);
            due.forEach((timer) => timer.fn());
            await Promise.resolve();
            await Promise.resolve();
        }
    }

    test("returns the bytes when the proxy serves the tile", async () => {
        stubFetch([200]);

        expect((await fetchOwnTile("/tiles/1/2/3")).byteLength).toBe(8);
    });

    test("asks again when the proxy is busy rather than unable", async () => {
        const fetched = stubFetch([503, 200]);

        const pending = fetchOwnTile("/tiles/1/2/3");
        await advanceRetries();

        expect((await pending).byteLength).toBe(8);
        expect(fetched.calls).toHaveLength(2);
    });

    /** A 404 is a real gap in the layer. Asking four more times gets the same answer more slowly. */
    test("believes a definitive answer the first time", async () => {
        const fetched = stubFetch([404]);

        await expect(fetchOwnTile("/tiles/1/2/3")).rejects.toThrow("404");
        expect(fetched.calls).toHaveLength(1);
    });

    test("gives up after the schedule is spent rather than asking forever", async () => {
        const fetched = stubFetch([503]);

        const pending = fetchOwnTile("/tiles/1/2/3");
        await advanceRetries();

        await expect(pending).rejects.toThrow("503");
        expect(fetched.calls).toHaveLength(5);
    });

    /**
     * The refusal carries the only figure anyone actually knows - the proxy's own slot hold time -
     * so a schedule that ignores it spends attempts on windows the proxy has already said are busy.
     */
    test("waits out the Retry-After the refusal carried", async () => {
        // Longer than any delay the schedule picks for a first retry (1000ms, jittered to at most
        // 1500), and short enough that `advanceRetries` still recognises it as a retry.
        stubFetch([503, 200], 5);

        const pending = fetchOwnTile("/tiles/1/2/3");
        for (let turn = 0; turn < 8; turn++) await Promise.resolve();
        const waits = scheduled.filter((timer) => timer.ms < 20_000).map((timer) => timer.ms);
        await advanceRetries();
        await pending;

        expect(waits.length).toBeGreaterThan(0);
        expect(Math.max(...waits)).toBeGreaterThanOrEqual(5_000);
    });

    test("survives a network error the same way it survives a 503", async () => {
        const fetched = stubFetch(["network-error", 200]);

        const pending = fetchOwnTile("/tiles/1/2/3");
        await advanceRetries();

        expect((await pending).byteLength).toBe(8);
        expect(fetched.calls).toHaveLength(2);
    });

    /** A slot held by a failed attempt is a slot the rest of the viewport queues behind for nothing. */
    test("hands its slot back on every outcome", async () => {
        stubFetch([404]);
        await expect(fetchOwnTile("/tiles/1/2/3")).rejects.toThrow();

        const held = await Promise.all(Array.from({ length: 6 }, () => acquireOwnTileSlot()));

        expect(held).toHaveLength(6);
        held.forEach((release) => release());
    });

    /**
     * A tile the map has moved past must stop occupying a slot the tiles still on screen are queued
     * behind - including during a backoff, which is where it would otherwise spend most of its life.
     */
    test("stops waiting when the map aborts the tile", async () => {
        const fetched = stubFetch([503]);
        const controller = new AbortController();
        const pending = fetchOwnTile("/tiles/1/2/3", controller.signal);
        await Promise.resolve();
        controller.abort();
        await advanceRetries();

        await expect(pending).rejects.toThrow();
        expect(fetched.calls).toHaveLength(1);
    });
});

/** The exporter draws what it got: one tile it could not have must cost one square, not the file. */
describe("loadOwnTileImage", () => {
    test("resolves null rather than rejecting when the tile cannot be had", async () => {
        stubFetch([404]);

        expect(await loadOwnTileImage("/tiles/1/2/3")).toBeNull();
    });
});
