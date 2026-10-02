import { beforeEach, describe, expect, test } from "bun:test";

import { orderSaveHandlers } from "./sortable-order";

let list: HTMLElement;
const ids = (): string[] => Array.from(list.children).map((el) => (el as HTMLElement).dataset.id ?? "");

/** What a drag does: Sortable says it started, moves the item, then says it ended. */
function drag(handlers: { onStart: () => void; onEnd: () => void }, id: string, beforeId: string | null): void {
    handlers.onStart();
    const item = list.querySelector(`[data-id="${id}"]`)!;
    list.insertBefore(item, beforeId ? list.querySelector(`[data-id="${beforeId}"]`) : null);
    handlers.onEnd();
}

const settle = async (): Promise<void> => {
    for (let i = 0; i < 5; i++) await Promise.resolve();
};

beforeEach(() => {
    document.body.innerHTML = '<ol id="list"><li data-id="1"></li><li data-id="2"></li><li data-id="3"></li></ol>';
    list = document.getElementById("list")!;
});

describe("saving the order a drag left a list in", () => {
    test("a failed save puts the list back in the order the server still has", async () => {
        const failures: unknown[] = [];
        const handlers = orderSaveHandlers(list, () => Promise.reject(new Error("refused")), (error) => void failures.push(error));

        drag(handlers, "3", "1");
        expect(ids()).toEqual(["3", "1", "2"]);
        await settle();

        expect(ids()).toEqual(["1", "2", "3"]);
        expect(failures).toHaveLength(1);
    });

    test("a saved order stays", async () => {
        const handlers = orderSaveHandlers(list, () => Promise.resolve(), () => undefined);

        drag(handlers, "3", "1");
        await settle();

        expect(ids()).toEqual(["3", "1", "2"]);
    });

    test("a failure that a later drag has superseded leaves the later order alone", async () => {
        const saves: Array<{ resolve: () => void; reject: () => void }> = [];
        const handlers = orderSaveHandlers(
            list,
            () => new Promise<void>((resolve, reject) => saves.push({ resolve, reject: () => reject(new Error("refused")) })),
            () => undefined,
        );

        drag(handlers, "3", "1");
        drag(handlers, "1", null);
        expect(ids()).toEqual(["3", "2", "1"]);
        saves[0]!.reject();
        saves[1]!.resolve();
        await settle();

        expect(ids()).toEqual(["3", "2", "1"]);
    });

    test("when the later save fails too, the list goes back to the last order a save landed", async () => {
        const saves: Array<{ resolve: () => void; reject: () => void }> = [];
        const handlers = orderSaveHandlers(
            list,
            () => new Promise<void>((resolve, reject) => saves.push({ resolve, reject: () => reject(new Error("refused")) })),
            () => undefined,
        );

        drag(handlers, "3", "1");
        saves[0]!.resolve();
        await settle();
        drag(handlers, "1", null);
        drag(handlers, "2", "3");
        saves[1]!.reject();
        saves[2]!.reject();
        await settle();

        expect(ids()).toEqual(["3", "1", "2"]);
    });
});
