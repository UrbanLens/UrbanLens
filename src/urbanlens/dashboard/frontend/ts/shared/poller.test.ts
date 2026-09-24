import { afterEach, beforeEach, describe, expect, jest, test } from "bun:test";

import { startPoller, type PollerHandle } from "./poller";

let visibility: DocumentVisibilityState = "visible";
const handles: PollerHandle[] = [];

function poll(tick: () => unknown, options: Parameters<typeof startPoller>[1]): PollerHandle {
    const handle = startPoller(tick, options);
    handles.push(handle);
    return handle;
}

function setVisibility(state: DocumentVisibilityState): void {
    visibility = state;
    document.dispatchEvent(new Event("visibilitychange"));
}

async function settle(): Promise<void> {
    for (let i = 0; i < 10; i++) await Promise.resolve();
}

async function advance(ms: number): Promise<void> {
    jest.advanceTimersByTime(ms);
    await settle();
}

beforeEach(() => {
    jest.useFakeTimers();
    visibility = "visible";
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
    document.body.innerHTML = "";
});

afterEach(() => {
    handles.splice(0).forEach((handle) => handle.stop());
    Reflect.deleteProperty(document, "visibilityState");
    jest.useRealTimers();
});

describe("startPoller", () => {
    test("ticks once per interval", async () => {
        let ticks = 0;
        poll(() => ticks++, { intervalMs: 1000 });
        await advance(999);
        expect(ticks).toBe(0);
        await advance(1);
        expect(ticks).toBe(1);
        await advance(3000);
        expect(ticks).toBe(4);
    });

    test("immediate ticks without waiting a full interval", async () => {
        let ticks = 0;
        poll(() => ticks++, { intervalMs: 1000, immediate: true });
        await advance(0);
        expect(ticks).toBe(1);
    });

    test("stops for good once its element leaves the document", async () => {
        document.body.innerHTML = '<div id="map"></div>';
        const element = document.getElementById("map")!;
        let ticks = 0;
        const handle = poll(() => ticks++, { intervalMs: 1000, element });
        await advance(1000);
        expect(ticks).toBe(1);

        // What an hx-boost body swap does to the map container.
        document.body.innerHTML = "<main>another page</main>";
        await advance(5000);
        expect(ticks).toBe(1);
        expect(handle.stopped).toBe(true);
    });

    test("does nothing while the page is hidden", async () => {
        let ticks = 0;
        poll(() => ticks++, { intervalMs: 1000 });
        setVisibility("hidden");
        await advance(10_000);
        expect(ticks).toBe(0);
    });

    test("on return, a tick that fell due while hidden runs at once", async () => {
        let ticks = 0;
        poll(() => ticks++, { intervalMs: 1000 });
        setVisibility("hidden");
        await advance(10_000);
        setVisibility("visible");
        await advance(0);
        expect(ticks).toBe(1);
        await advance(1000);
        expect(ticks).toBe(2);
    });

    test("a short absence does not tick early", async () => {
        let ticks = 0;
        poll(() => ticks++, { intervalMs: 1000 });
        await advance(200);
        setVisibility("hidden");
        await advance(300);
        setVisibility("visible");
        await advance(499);
        expect(ticks).toBe(0);
        await advance(1);
        expect(ticks).toBe(1);
    });

    test("pagehide pauses it and pageshow resumes it", async () => {
        let ticks = 0;
        poll(() => ticks++, { intervalMs: 1000 });
        window.dispatchEvent(new Event("pagehide"));
        await advance(5000);
        expect(ticks).toBe(0);
        window.dispatchEvent(new Event("pageshow"));
        await advance(0);
        expect(ticks).toBe(1);
    });

    test("a slow tick is never overlapped by the next", async () => {
        let running = 0;
        let peak = 0;
        let ticks = 0;
        const release: Array<() => void> = [];
        poll(
            () =>
                new Promise<void>((resolve) => {
                    ticks++;
                    running++;
                    peak = Math.max(peak, running);
                    release.push(() => {
                        running--;
                        resolve();
                    });
                }),
            { intervalMs: 1000 },
        );
        await advance(1000);
        await advance(10_000);
        expect(ticks).toBe(1);
        release.shift()!();
        await settle();
        await advance(1000);
        expect(ticks).toBe(2);
        expect(peak).toBe(1);
    });

    test("a failing tick does not end the poll", async () => {
        let ticks = 0;
        poll(
            () => {
                ticks++;
                throw new Error("offline");
            },
            { intervalMs: 1000 },
        );
        await advance(1000);
        await advance(1000);
        expect(ticks).toBe(2);
    });

    test("stop() ends it and drops its listeners", async () => {
        let ticks = 0;
        const handle = poll(() => ticks++, { intervalMs: 1000 });
        handle.stop();
        setVisibility("hidden");
        setVisibility("visible");
        window.dispatchEvent(new Event("pageshow"));
        await advance(5000);
        expect(ticks).toBe(0);
    });
});
