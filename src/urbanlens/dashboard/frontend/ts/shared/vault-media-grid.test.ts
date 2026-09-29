/**
 * A Vault gallery grid: what counts as a loaded tile, re-paging on a sort change, and settling placeholders.
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { DOCUMENTS } from "./vault-document-grid";
import { renderVaultSkeletonTile, settleVaultTile, VaultGrid, vaultTileSelector } from "./vault-media-grid";
import { PHOTOS, renderVaultPhotoTile } from "./vault-photo-grid";

describe.each([PHOTOS, DOCUMENTS])("$plural tiles", (kind) => {
    test("a skeleton tile carries the base class but no data-id", () => {
        // Both halves matter: sharing the base class is why the filter is needed, and the missing data-id is what it keys on.
        const skeleton = renderVaultSkeletonTile(kind);
        expect(skeleton.classList.contains(`${kind.kind}-tile`)).toBe(true);
        expect(skeleton.hasAttribute("data-id")).toBe(false);
    });

    test("the loaded-tile selector does not match a skeleton", () => {
        document.body.replaceChildren(renderVaultSkeletonTile(kind));
        expect(document.querySelectorAll(vaultTileSelector(kind))).toHaveLength(0);
    });
});

describe("VaultGrid", () => {
    const realFetch = globalThis.fetch;
    const realObserver = (globalThis as Record<string, unknown>).IntersectionObserver;
    let intersect: (() => void) | null;
    let fetched: string[];

    beforeEach(() => {
        intersect = null;
        fetched = [];
        (globalThis as Record<string, unknown>).IntersectionObserver = class {
            constructor(callback: (entries: { isIntersecting: boolean }[]) => void) {
                intersect = () => callback([{ isIntersecting: true }]);
            }
            observe(): void {}
            disconnect(): void {}
        };
        globalThis.fetch = (async (input: RequestInfo | URL) => {
            fetched.push(String(input));
            return new Response(JSON.stringify({ items: [{ id: 9, url: "/m/9" }] }));
        }) as typeof fetch;
        document.body.innerHTML =
            '<select id="vault-photos-sort"><option value="recent" selected>Recent</option><option value="name">Name</option></select>' +
            '<ul id="photo-grid" data-items-url="/vault/photos/items/" data-photo-count="2">' +
            '<li class="photo-tile" data-id="1"></li><li class="photo-tile" data-id="2"></li></ul>';
    });

    afterEach(() => {
        globalThis.fetch = realFetch;
        (globalThis as Record<string, unknown>).IntersectionObserver = realObserver;
        document.body.innerHTML = "";
    });

    function grid(): VaultGrid {
        const found = VaultGrid.find({ kind: PHOTOS, renderTile: renderVaultPhotoTile, imageSelector: null, extraParams: { show: "from_others" } });
        if (!found) throw new Error("no grid");
        return found;
    }

    test("changing the sort drops the loaded tiles and pages from the start under the new sort", async () => {
        grid().init();
        expect(fetched).toEqual([]);

        const select = document.getElementById("vault-photos-sort") as HTMLSelectElement;
        select.value = "name";
        select.dispatchEvent(new Event("change"));
        expect(document.querySelectorAll(".photo-tile[data-id]")).toHaveLength(0);
        intersect?.();
        await new Promise((resolve) => setTimeout(resolve, 0));

        const params = new URL(fetched[0]!, "https://urbanlens.test").searchParams;
        expect([params.get("offset"), params.get("sort"), params.get("show")]).toEqual(["0", "name", "from_others"]);
        expect(document.querySelector(".photo-tile[data-id]")?.id).toBe("photo-tile-9");
    });

    test("a settled placeholder is swapped for its tile, and one that is gone is dropped", () => {
        const pending = document.createElement("li");
        document.getElementById("photo-grid")!.append(pending);
        settleVaultTile(pending, { id: 5, url: "/m/5" }, renderVaultPhotoTile);
        expect(document.getElementById("photo-tile-5")).not.toBeNull();
        settleVaultTile(document.getElementById("photo-tile-5")!, null, renderVaultPhotoTile);
        expect(document.getElementById("photo-tile-5")).toBeNull();
    });
});
