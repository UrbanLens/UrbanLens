import { afterEach, describe, expect, test } from "bun:test";

import { KeyedDebounce } from "./keyed-debounce";

const pending: KeyedDebounce<string>[] = [];

function debounce(delayMs = 10_000): { saved: [string, string][]; saves: KeyedDebounce<string> } {
    const saved: [string, string][] = [];
    const saves = new KeyedDebounce<string>(delayMs, (key, value) => saved.push([key, value]));
    pending.push(saves);
    return { saved, saves };
}

afterEach(() => {
    pending.splice(0).forEach((saves) => saves.dispose());
});

describe("KeyedDebounce", () => {
    test("each key keeps only its newest value, and keys don't share a slot", async () => {
        const { saved, saves } = debounce(5);

        saves.schedule("a", "1");
        saves.schedule("b", "x");
        saves.schedule("a", "2");
        await new Promise((resolve) => setTimeout(resolve, 20));

        expect(saved.sort()).toEqual([
            ["a", "2"],
            ["b", "x"],
        ]);
    });

    test("leaving the page saves what is still waiting", () => {
        const { saved, saves } = debounce();

        saves.schedule("a", "1");
        window.dispatchEvent(new Event("pagehide"));

        expect(saved).toEqual([["a", "1"]]);
        saves.flush();
        expect(saved).toHaveLength(1);
    });

    test("a cancelled key is never saved", () => {
        const { saved, saves } = debounce();

        saves.schedule("a", "1");
        saves.cancel("a");
        saves.flush();

        expect(saved).toEqual([]);
    });

    test("flushing one key leaves the others waiting", () => {
        const { saved, saves } = debounce();

        saves.schedule("a", "1");
        saves.schedule("b", "2");
        saves.flush("a");

        expect(saved).toEqual([["a", "1"]]);
    });
});
