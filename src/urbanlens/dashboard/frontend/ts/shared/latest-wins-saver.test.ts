import { describe, expect, test } from "bun:test";

import { LatestWinsSaver } from "./latest-wins-saver";

/** A send whose calls settle only when the test says so. */
function controlledSend() {
    const calls: { value: string; resolve: () => void; reject: (err: Error) => void }[] = [];
    const send = (value: string) =>
        new Promise<void>((resolve, reject) => {
            calls.push({ value, resolve, reject });
        });
    return { calls, send };
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("LatestWinsSaver", () => {
    test("only one save is in flight, and the newest waiting value is the one sent next", async () => {
        const { calls, send } = controlledSend();
        const saver = new LatestWinsSaver<string>({ send, onSaved: () => {}, onFailed: () => {} }, "a");

        saver.request("b");
        saver.request("c");
        saver.request("d");
        expect(calls.map((c) => c.value)).toEqual(["b"]);

        calls[0]!.resolve();
        await settle();

        expect(calls.map((c) => c.value)).toEqual(["b", "d"]);
    });

    test("a failure with a newer value waiting sends that value instead of rolling back", async () => {
        const { calls, send } = controlledSend();
        const failed: (string | null)[] = [];
        const saver = new LatestWinsSaver<string>({ send, onSaved: () => {}, onFailed: (_err, confirmed) => failed.push(confirmed) }, "a");

        saver.request("b");
        saver.request("c");
        calls[0]!.reject(new Error("boom"));
        await settle();

        expect(failed).toEqual([]);
        expect(calls.map((c) => c.value)).toEqual(["b", "c"]);
    });

    test("a failure with nothing newer rolls back to the last value the server confirmed", async () => {
        const { calls, send } = controlledSend();
        const failed: (string | null)[] = [];
        const saver = new LatestWinsSaver<string>({ send, onSaved: () => {}, onFailed: (_err, confirmed) => failed.push(confirmed) }, "a");

        saver.request("b");
        calls[0]!.resolve();
        await settle();
        saver.request("c");
        calls[1]!.reject(new Error("boom"));
        await settle();

        expect(failed).toEqual(["b"]);
    });

    test("success is reported once, for the final value", async () => {
        const { calls, send } = controlledSend();
        const saved: string[] = [];
        const saver = new LatestWinsSaver<string>({ send, onSaved: (value) => saved.push(value), onFailed: () => {} }, "a");

        saver.request("b");
        saver.request("c");
        calls[0]!.resolve();
        await settle();
        calls[1]!.resolve();
        await settle();

        expect(saved).toEqual(["c"]);
    });
});
