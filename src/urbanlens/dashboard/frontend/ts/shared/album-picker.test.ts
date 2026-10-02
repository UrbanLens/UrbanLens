/**
 * The add-to-album picker loads its rows from the server every time it opens, rather than carrying them in the page (P171).
 */

import { afterEach, describe, expect, it } from "bun:test";

import { openAlbumPicker } from "./album-picker";

const realHtmx = window.htmx;
const realFetch = globalThis.fetch;
const realToastr = window.toastr;

afterEach(() => {
    window.htmx = realHtmx;
    globalThis.fetch = realFetch;
    window.toastr = realToastr;
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

describe("picking an album", () => {
    function pick(respond: () => Promise<Response>): { sent: { url: string; body: string; csrf: string }[]; toasts: string[]; dialog: HTMLDialogElement } {
        const sent: { url: string; body: string; csrf: string }[] = [];
        const toasts: string[] = [];
        window.htmx = { process: () => {}, trigger: () => {}, ajax: async () => {} };
        window.csrftoken = "tok";
        window.toastr = Object.assign(Object.create(null), {
            success: (m: string) => toasts.push(`success:${m}`),
            error: (m: string) => toasts.push(`error:${m}`),
            info: (m: string) => toasts.push(`info:${m}`),
            warning: (m: string) => toasts.push(`warning:${m}`),
            clear: () => undefined,
        });
        globalThis.fetch = Object.assign(
            async (input: RequestInfo | URL, init?: RequestInit) => {
                sent.push({ url: String(input), body: String(init?.body ?? ""), csrf: new Headers(init?.headers).get("X-CSRFToken") ?? "" });
                return respond();
            },
            { preconnect: realFetch.preconnect },
        );
        document.body.innerHTML = `<dialog id="album-target-dialog" data-picker-url="/albums/?picker=1"><ul class="album-target-list"></ul></dialog>`;
        const dialog = document.getElementById("album-target-dialog") as HTMLDialogElement;
        dialog.showModal = () => dialog.setAttribute("open", "");
        dialog.close = () => dialog.removeAttribute("open");
        openAlbumPicker({ imageIds: [4, 5] });
        // The rows htmx swaps in once the picker opens.
        dialog.querySelector(".album-target-list")!.innerHTML = `<li class="album-target-item" data-add-url="/albums/ruins/add/"><button data-album-target>Ruins</button></li>`;
        dialog.querySelector<HTMLButtonElement>("[data-album-target]")!.click();
        return { sent, toasts, dialog };
    }

    const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

    it("adds the photos, says so, and closes", async () => {
        const { sent, toasts, dialog } = pick(async () => new Response(JSON.stringify({ ok: true }), { status: 200 }));
        await settle();
        expect(sent).toEqual([{ url: "/albums/ruins/add/", body: '{"image_ids":[4,5]}', csrf: "tok" }]);
        expect(toasts).toEqual(["success:Added 2 photos to the album."]);
        expect(dialog.open).toBe(false);
    });

    it("a refusal shows the server's reason and leaves the dialog open for another pick", async () => {
        const { toasts, dialog } = pick(async () => new Response(JSON.stringify({ error: "That album is not yours." }), { status: 403 }));
        await settle();
        expect(toasts).toEqual(["error:That album is not yours."]);
        expect(dialog.open).toBe(true);
    });
});
