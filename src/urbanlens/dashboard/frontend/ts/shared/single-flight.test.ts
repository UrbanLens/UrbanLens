import { describe, expect, test } from "bun:test";

import { singleFlight } from "./single-flight";

interface Gate<T> {
    promise: Promise<T>;
    resolve: (value: T) => void;
    reject: (error: unknown) => void;
}

function gate<T>(): Gate<T> {
    let resolve!: (value: T) => void;
    let reject!: (error: unknown) => void;
    const promise = new Promise<T>((res, rej) => {
        resolve = res;
        reject = rej;
    });
    return { promise, resolve, reject };
}

function nth<T>(items: T[], index: number): T {
    const item = items[index];
    if (item === undefined) throw new Error(`no run #${index} was started`);
    return item;
}

async function flush(): Promise<void> {
    for (let i = 0; i < 10; i++) await Promise.resolve();
}

/** A task whose runs each wait on their own gate, recording how many are in flight at once. */
function controlledTask(): { task: () => Promise<number>; gates: Gate<number>[]; maxConcurrent: () => number } {
    const gates: Gate<number>[] = [];
    let running = 0;
    let peak = 0;
    const task = async (): Promise<number> => {
        running += 1;
        peak = Math.max(peak, running);
        const g = gate<number>();
        gates.push(g);
        try {
            return await g.promise;
        } finally {
            running -= 1;
        }
    };
    return { task, gates, maxConcurrent: () => peak };
}

describe("singleFlight", () => {
    test("a call while idle starts a run straight away", () => {
        const { task, gates } = controlledTask();
        void singleFlight(task)();
        expect(gates).toHaveLength(1);
    });

    test("calls during a run never start a second run in parallel", async () => {
        const { task, gates, maxConcurrent } = controlledTask();
        const run = singleFlight(task);
        void run();
        void run();
        void run();
        expect(gates).toHaveLength(1);
        nth(gates, 0).resolve(1);
        await flush();
        nth(gates, 1).resolve(2);
        await flush();
        expect(maxConcurrent()).toBe(1);
    });

    test("any number of mid-flight calls share exactly one trailing run", async () => {
        const { task, gates } = controlledTask();
        const run = singleFlight(task);
        const first = run();
        const joined = [run(), run(), run(), run()];
        expect(new Set(joined).size).toBe(1);
        nth(gates, 0).resolve(1);
        await flush();
        expect(gates).toHaveLength(2);
        nth(gates, 1).resolve(2);
        expect(await first).toBe(1);
        expect(await Promise.all(joined)).toEqual([2, 2, 2, 2]);
        await flush();
        expect(gates).toHaveLength(2);
    });

    test("a slower, older response cannot overwrite the state a newer call asked for", async () => {
        // The map's case: a refresh is in flight, the user deletes a pin, and a second refresh is requested.
        let server = ["a", "b"];
        let applied: string[] = [];
        const responses: Gate<void>[] = [];
        const refresh = singleFlight(async () => {
            const snapshot = [...server];
            const response = gate<void>();
            responses.push(response);
            await response.promise;
            applied = snapshot;
        });

        const older = refresh();
        server = ["a"];
        const newer = refresh();
        // Unguarded, both requests are out and the newer one answers first.
        responses[1]?.resolve();
        nth(responses, 0).resolve();
        await older;
        await flush();
        nth(responses, 1).resolve();
        await newer;

        expect(applied).toEqual(["a"]);
    });

    test("a run that fails still lets the trailing run go ahead, and rejects only its own callers", async () => {
        const { task, gates } = controlledTask();
        const run = singleFlight(task);
        const first = run();
        const second = run();
        nth(gates, 0).reject(new Error("offline"));
        await expect(first).rejects.toThrow("offline");
        await flush();
        nth(gates, 1).resolve(7);
        expect(await second).toBe(7);
    });

    test("a task that throws synchronously rejects rather than wedging the gate", async () => {
        let calls = 0;
        const run = singleFlight((): Promise<number> => {
            calls += 1;
            if (calls === 1) throw new Error("sync");
            return Promise.resolve(calls);
        });
        await expect(run()).rejects.toThrow("sync");
        expect(await run()).toBe(2);
    });

    test("once everything has settled, the next call starts a fresh run", async () => {
        const { task, gates } = controlledTask();
        const run = singleFlight(task);
        const first = run();
        nth(gates, 0).resolve(1);
        await first;
        const next = run();
        expect(gates).toHaveLength(2);
        nth(gates, 1).resolve(2);
        expect(await next).toBe(2);
    });
});
