import { describe, expect, test } from "bun:test";

import { retryWhileCurrent } from "./retry-while-current";

function recorder(): { waits: number[]; wait: (ms: number) => Promise<void> } {
    const waits: number[] = [];
    return { waits, wait: (ms) => (waits.push(ms), Promise.resolve()) };
}

describe("retryWhileCurrent", () => {
    test("stops at the first success", async () => {
        const { waits, wait } = recorder();
        let calls = 0;

        const done = await retryWhileCurrent(async () => ++calls === 3, { isCurrent: () => true, wait });

        expect(done).toBe(true);
        expect(calls).toBe(3);
        expect(waits).toEqual([2000, 4000]);
    });

    test("doubles the delay up to the cap", async () => {
        const { waits, wait } = recorder();
        let calls = 0;

        await retryWhileCurrent(async () => ++calls === 6, { isCurrent: () => true, wait, firstDelayMs: 1000, maxDelayMs: 5000 });

        expect(waits).toEqual([1000, 2000, 4000, 5000, 5000]);
    });

    test("gives up once the step is no longer current", async () => {
        const { wait } = recorder();
        let calls = 0;

        const done = await retryWhileCurrent(async () => (++calls, false), { isCurrent: () => calls < 2, wait });

        expect(done).toBe(false);
        expect(calls).toBe(2);
    });

    test("never runs a step that is already stale", async () => {
        let calls = 0;

        await retryWhileCurrent(async () => (++calls, true), { isCurrent: () => false });

        expect(calls).toBe(0);
    });
});
