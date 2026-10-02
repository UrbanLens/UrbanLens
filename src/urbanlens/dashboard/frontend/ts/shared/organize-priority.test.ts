import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { initOrganizePriority } from "./organize-priority";

function itemHtml(id: string): string {
    return `<div class="priority-item" data-id="${id}" data-kind="tag">
        <button type="button" class="priority-jump-btn" data-priority-jump="top">top</button>
        <span class="priority-order-chip">1</span>
    </div>`;
}

const settle = async () => {
    for (let i = 0; i < 5; i++) await Promise.resolve();
};

describe("organize-priority failed save", () => {
    const realFetch = globalThis.fetch;
    let fail: () => void = () => {};

    beforeEach(() => {
        document.body.innerHTML = `
          <div id="panel-priority">
            <div id="priority-list" class="priority-list" data-save-url="/organize/priority/save/">${itemHtml("1")}${itemHtml("2")}${itemHtml("3")}</div>
          </div>`;
        (window as unknown as { _orgRegisterSelectionClearer: (fn: () => void) => void })._orgRegisterSelectionClearer = () => {};
        globalThis.fetch = (() =>
            new Promise((_resolve, reject) => {
                fail = () => reject(new Error("boom"));
            })) as unknown as typeof fetch;
        initOrganizePriority();
    });

    afterEach(() => {
        globalThis.fetch = realFetch;
    });

    const ids = () => Array.from(document.querySelectorAll<HTMLElement>("#priority-list .priority-item")).map((el) => el.dataset.id);

    test("puts the list back in the order the server still has", async () => {
        document.querySelector<HTMLElement>('.priority-item[data-id="3"] [data-priority-jump="top"]')!.click();
        expect(ids()).toEqual(["3", "1", "2"]);

        fail();
        await settle();

        expect(ids()).toEqual(["1", "2", "3"]);
    });

    test("a refusal toasts the server's sentence, not its JSON", async () => {
        const realToastr = window.toastr;
        const toasts: string[] = [];
        window.toastr = Object.assign(Object.create(null), {
            success: (m: string) => toasts.push(`success:${m}`),
            error: (m: string) => toasts.push(`error:${m}`),
            info: (m: string) => toasts.push(`info:${m}`),
            warning: (m: string) => toasts.push(`warning:${m}`),
            clear: () => undefined,
        });
        globalThis.fetch = Object.assign(async () => new Response(JSON.stringify({ error: "Reorder at most 50 labels at a time." }), { status: 400 }), {
            preconnect: realFetch.preconnect,
        });
        try {
            document.querySelector<HTMLElement>('.priority-item[data-id="3"] [data-priority-jump="top"]')!.click();
            await new Promise((resolve) => setTimeout(resolve, 0));
            expect(toasts).toEqual(["error:Save failed: Reorder at most 50 labels at a time."]);
            expect(ids()).toEqual(["1", "2", "3"]);
        } finally {
            window.toastr = realToastr;
        }
    });

    test("does not bring back an item the list was re-rendered without while the save was in flight", async () => {
        document.querySelector<HTMLElement>('.priority-item[data-id="3"] [data-priority-jump="top"]')!.click();
        document.getElementById("priority-list")!.innerHTML = itemHtml("3") + itemHtml("1");

        fail();
        await settle();

        expect(ids()).toEqual(["1", "3"]);
    });
});
