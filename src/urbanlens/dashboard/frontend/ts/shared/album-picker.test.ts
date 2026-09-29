/**
 * The add-to-album picker loads its rows from the server every time it opens, rather than carrying them in the page (P171).
 */

import { afterEach, describe, expect, it } from "bun:test";

import { openAlbumPicker } from "./album-picker";

const realHtmx = window.htmx;

afterEach(() => {
    window.htmx = realHtmx;
    document.body.innerHTML = "";
});

describe("openAlbumPicker", () => {
    it("clears the last search and asks the server for the first page of rows", () => {
        const calls: { verb: string; url: string; target: unknown; swap: unknown }[] = [];
        window.htmx = {
            process: () => {},
            trigger: () => {},
            ajax: async (verb: string, url: string, options: Record<string, unknown>) => {
                calls.push({ verb, url, target: options.target, swap: options.swap });
            },
        };
        document.body.innerHTML = `
            <dialog id="album-target-dialog" data-picker-url="/albums/?children=1&picker=1">
              <h4 class="album-target-title"></h4>
              <input class="album-target-search" value="old search">
              <ul class="album-target-list"><li class="album-target-item">stale</li></ul>
            </dialog>`;
        const dlg = document.getElementById("album-target-dialog") as HTMLDialogElement;
        dlg.showModal = () => {};
        const list = dlg.querySelector(".album-target-list");

        openAlbumPicker({ imageIds: [1], moveFrom: "interior" });

        expect(calls).toEqual([{ verb: "GET", url: "/albums/?children=1&picker=1", target: list, swap: "innerHTML" }]);
        expect(dlg.querySelector<HTMLInputElement>(".album-target-search")?.value).toBe("");
        expect(list?.querySelector(".album-target-item")).toBeNull();
        expect(dlg.querySelector(".album-target-title")?.textContent).toBe("Move to album");
    });
});
