/**
 * Paged Organize rows (P66): the parts of the page that need every row of a kind load the rest first.
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import type { HtmxApi } from "../types/globals";
import { applyOrgFilter, installOrgFilterEngine } from "./organize-filter-engine";
import { createOrganizeHeader, installOrgBulkToolbar, orgHeader } from "./organize-header";
import { hasUnloadedRows, installRowsLoadedHeader, loadAllRows, ROWS_LOADED_HEADER, rowsLoadedHeaderValue } from "./organize-rows-paging";
import { initOrganizeTabs } from "./organize-tabs";

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

function card(ns: string, id: number, name: string, parents = ""): string {
    return `<div class="tag-card" id="${ns}-card-${id}" data-id="${id}" data-${ns}-id="${id}" data-${ns}-name="${name}" data-${ns}-color="" data-${ns}-icon="" data-${ns}-pin-count="0" data-${ns}-parents="${parents}">
      <label class="ul-checkbox-wrap"><input type="checkbox" class="${ns === "category" ? "cat" : ns}-select-cb" data-${ns}-id="${id}" data-id="${id}"></label></div>`;
}

function sentinel(kind: string, after: number): string {
    return `<div class="organize-rows-more" hx-get="/rows/${kind}/?after_id=${after}" hx-trigger="intersect once" hx-target="this" hx-swap="outerHTML" data-rest-url="/rows/${kind}/?after_id=${after}&amp;all=1">Loading more</div>`;
}

/** Fires the events htmx 1.9.11 fires for a swap, in its order: afterSwap on each inserted element (aimed at the
 * original target), then afterRequest and afterOnLoad on the requesting element, and again on its nearest
 * connected ancestor when the swap removed it. */
function swapLikeHtmx(source: HTMLElement, target: HTMLElement, swap: string, html: string): void {
    const ancestors: HTMLElement[] = [];
    for (let el = source.parentElement; el; el = el.parentElement) ancestors.push(el);
    const detail = { target };
    if (swap === "outerHTML") {
        const template = document.createElement("template");
        template.innerHTML = html;
        const inserted = Array.from(template.content.children);
        target.replaceWith(template.content);
        inserted.forEach((el) => el.dispatchEvent(new CustomEvent("htmx:afterSwap", { bubbles: true, detail })));
    } else {
        target.innerHTML = html;
        target.dispatchEvent(new CustomEvent("htmx:afterSwap", { bubbles: true, detail }));
    }
    for (const name of ["htmx:afterRequest", "htmx:afterOnLoad"]) source.dispatchEvent(new CustomEvent(name, { bubbles: true, detail }));
    if (!source.isConnected) {
        const parent = ancestors.find((el) => el.isConnected);
        for (const name of ["htmx:afterRequest", "htmx:afterOnLoad"]) parent?.dispatchEvent(new CustomEvent(name, { bubbles: true, detail }));
    }
}

interface AjaxCall {
    verb: string;
    url: string;
    source: HTMLElement;
    target: HTMLElement;
    swap: string;
}

/** A stand-in for htmx.ajax that answers from *responses* by URL, or not at all for one missing there. */
function fakeHtmx(responses: Record<string, string>, gate?: Promise<void>): AjaxCall[] {
    const calls: AjaxCall[] = [];
    const htmx: HtmxApi = {
        process: () => {},
        trigger: () => {},
        ajax: async (verb, url, options) => {
            const source = (options.source as HTMLElement | undefined) ?? document.body;
            const target = options.target as HTMLElement;
            const swap = String(options.swap);
            calls.push({ verb, url, source, target, swap });
            if (gate) await gate;
            if (url in responses) swapLikeHtmx(source, target, swap, responses[url]!);
        },
    };
    window.htmx = htmx;
    return calls;
}

afterEach(() => {
    delete window.htmx;
});

describe("rowsLoadedHeaderValue", () => {
    test("a tab still loading asks for nothing in particular", () => {
        document.body.innerHTML = '<div id="r" class="organize-label-rows"><div class="organize-section-loading"></div></div>';
        expect(rowsLoadedHeaderValue(document.getElementById("r")!)).toBeUndefined();
    });

    test("a tab with pages to come asks for the cards it has, not the tree view's copies of them", () => {
        document.body.innerHTML = `<div id="r" class="organize-label-rows">${card("tag", 1, "a")}${card("tag", 2, "b")}${sentinel("tag", 2)}<div class="tag-tree-root">${card("tag", 1, "a")}</div></div>`;
        expect(rowsLoadedHeaderValue(document.getElementById("r")!)).toBe("2");
    });

    test("a tab with every row asks for every row", () => {
        document.body.innerHTML = `<div id="r" class="organize-label-rows">${card("tag", 1, "a")}</div>`;
        expect(rowsLoadedHeaderValue(document.getElementById("r")!)).toBe("all");
    });

    test("htmx requests that re-render a rows container carry it, and others do not", () => {
        installRowsLoadedHeader();
        document.body.innerHTML = `<div id="r" class="organize-label-rows">${card("tag", 1, "a")}${sentinel("tag", 1)}</div><div id="elsewhere"></div>`;
        const configure = (target: Element) => {
            const detail = { target, headers: {} as Record<string, string> };
            document.body.dispatchEvent(new CustomEvent("htmx:configRequest", { bubbles: true, detail }));
            return detail.headers;
        };
        expect(configure(document.getElementById("r")!)[ROWS_LOADED_HEADER]).toBe("1");
        expect(configure(document.getElementById("elsewhere")!)[ROWS_LOADED_HEADER]).toBeUndefined();
    });
});

describe("loadAllRows", () => {
    const REST = `${card("tag", 3, "c")}${card("tag", 4, "d")}`;

    beforeEach(() => {
        document.body.innerHTML = `<div id="r" class="organize-label-rows" data-rows-url="/rows/tag/">${card("tag", 1, "a")}${card("tag", 2, "b")}${sentinel("tag", 2)}</div>`;
    });

    const rows = () => document.getElementById("r")!;
    const ids = () => Array.from(rows().querySelectorAll<HTMLElement>(".tag-card")).map((c) => c.dataset.tagId);

    test("asks for the rest once, in place of the sentinel", async () => {
        const calls = fakeHtmx({ "/rows/tag/?after_id=2&all=1": REST });

        await loadAllRows(rows());

        expect(calls.map((c) => [c.url, c.swap])).toEqual([["/rows/tag/?after_id=2&all=1", "outerHTML"]]);
        expect(ids()).toEqual(["1", "2", "3", "4"]);
        expect(hasUnloadedRows(rows())).toBe(false);
    });

    test("callers that ask together share one request", async () => {
        const calls = fakeHtmx({ "/rows/tag/?after_id=2&all=1": REST });

        await Promise.all([loadAllRows(rows()), loadAllRows(rows()), loadAllRows(rows())]);

        expect(calls).toHaveLength(1);
    });

    test("waits for a next-page request already under way, then asks for what follows it", async () => {
        const calls = fakeHtmx({ "/rows/tag/?after_id=3&all=1": card("tag", 4, "d") });
        const inFlight = rows().querySelector<HTMLElement>(".organize-rows-more")!;
        inFlight.classList.add("htmx-request");

        const load = loadAllRows(rows());
        await tick();
        expect(calls).toHaveLength(0);
        swapLikeHtmx(inFlight, inFlight, "outerHTML", `${card("tag", 3, "c")}${sentinel("tag", 3)}`);
        await load;

        expect(calls.map((c) => c.url)).toEqual(["/rows/tag/?after_id=3&all=1"]);
        expect(ids()).toEqual(["1", "2", "3", "4"]);
    });

    test("a tab that has not loaded at all is loaded whole", async () => {
        rows().innerHTML = '<div class="organize-section-loading">Loading</div>';
        const calls = fakeHtmx({ "/rows/tag/?all=1": `${card("tag", 1, "a")}${REST}` });

        await loadAllRows(rows());

        expect(calls.map((c) => [c.url, c.swap, c.target.id])).toEqual([["/rows/tag/?all=1", "innerHTML", "r"]]);
        expect(ids()).toEqual(["1", "3", "4"]);
    });

    test("a server that will not send the rest is reported, not retried forever", async () => {
        const calls = fakeHtmx({});

        await expect(loadAllRows(rows())).rejects.toThrow();

        expect(calls).toHaveLength(1);
    });
});

/** The Organize page, tags active, with the categories tab loaded too, as organize/index.html renders them. */
function mountPage(tagRows: string, categoryRows: string): void {
    document.body.innerHTML = `
      <div class="organize-page" data-active-tab="tags">
        <button class="organize-tab active" data-tab="tags" data-filter-ns="tag"><span id="org-tab-count-tag" class="org-tab-count" hidden></span></button>
        <button class="organize-tab" data-tab="categories" data-filter-ns="cat"><span id="org-tab-count-cat" class="org-tab-count" hidden></span></button>
        <button id="org-header-sel-all"></button>
        <div id="panel-tags" class="organize-panel">
          <div id="tag-filter-bar" class="org-filter-bar" data-filter-ns="tag"><input id="tag-filter-search" class="org-filter-search"></div>
          <div id="tag-rows" class="organize-label-rows" data-rows-url="/rows/tag/" data-bulk-delete-url="/tag/bulk-delete/" data-bulk-edit-url="/tag/bulk-edit/" data-merge-url="/tag/merge/">${tagRows}</div>
          <div id="org-cross-tab-tag" hidden></div>
        </div>
        <div id="panel-categories" class="organize-panel" hidden>
          <div id="category-rows" class="organize-label-rows" data-rows-url="/rows/category/">${categoryRows}</div>
        </div>
      </div>
      <div id="org-bulk-bar"><span id="org-bulk-count"></span><button id="org-bulk-edit-btn"></button><button id="org-bulk-merge-btn"></button><button id="org-bulk-delete-btn"></button></div>
      <dialog id="tag-bulk-edit-dialog"><h2 id="tag-bulk-edit-title"></h2><input type="checkbox" id="tag-bulk-icon-nochange"><input type="checkbox" id="tag-bulk-color-nochange">
        <input id="icon-value-tag-bulk-edit"><div id="tag-bulk-color-picker"></div><input id="tag-bulk-color-value">
        <input type="checkbox" id="tag-bulk-order-nochange"><input id="tag-bulk-order-value"><input type="checkbox" id="tag-bulk-description-nochange"><textarea id="tag-bulk-description-value"></textarea>
        <button id="tag-bulk-edit-confirm"></button></dialog>`;
    installOrgBulkToolbar();
    createOrganizeHeader("tags");
    initOrganizeTabs();
    orgHeader.init();
}

describe("the Organize page with pages still to load", () => {
    const realFetch = globalThis.fetch;
    const realMatchMedia = window.matchMedia;
    let posts: Array<{ url: string; headers: Record<string, string> }> = [];

    beforeEach(() => {
        posts = [];
        globalThis.fetch = (async (url: string, init: RequestInit) => {
            posts.push({ url, headers: init.headers as Record<string, string> });
            return new Response(card("tag", 1, "alpha"), { status: 200, headers: { "Content-Type": "text/html" } });
        }) as unknown as typeof fetch;
        window.matchMedia = ((query: string) => ({ matches: false, media: query })) as unknown as typeof window.matchMedia;
        window.confirmDialog = async () => true;
        localStorage.removeItem("organize_view");
    });

    afterEach(() => {
        globalThis.fetch = realFetch;
        window.matchMedia = realMatchMedia;
        delete window.confirmDialog;
        const search = document.getElementById("tag-filter-search") as HTMLInputElement | null;
        if (search) {
            search.value = "";
            applyOrgFilter("tag");
        }
    });

    const firstPage = `${card("tag", 1, "alpha")}${card("tag", 2, "bravo")}${sentinel("tag", 2)}`;
    const rest = `${card("tag", 3, "charlie")}${card("tag", 4, "needle")}`;
    const selected = () => Array.from(document.querySelectorAll<HTMLElement>("#tag-rows .tag-card--selected")).map((c) => c.dataset.tagId);

    test("typing a filter finds a label that was not loaded yet", async () => {
        fakeHtmx({ "/rows/tag/?after_id=2&all=1": rest });
        mountPage(firstPage, card("category", 9, "elsewhere"));

        const search = document.getElementById("tag-filter-search") as HTMLInputElement;
        search.value = "needle";
        applyOrgFilter("tag");
        await tick();

        expect(document.getElementById("tag-card-4")?.style.display).toBe("");
        expect(document.getElementById("tag-card-1")?.style.display).toBe("none");
        expect(document.getElementById("tag-card-3")?.style.display).toBe("none");
    });

    test("another tab's count is marked as a lower bound until its rows are in", async () => {
        let release!: () => void;
        const gate = new Promise<void>((resolve) => {
            release = resolve;
        });
        fakeHtmx({ "/rows/category/?after_id=8&all=1": card("category", 7, "needle two") }, gate);
        installOrgFilterEngine();
        mountPage(card("tag", 4, "needle"), `${card("category", 8, "needle one")}${sentinel("category", 8)}`);

        const search = document.getElementById("tag-filter-search") as HTMLInputElement;
        search.value = "needle";
        applyOrgFilter("tag");
        await tick();
        expect(document.getElementById("org-tab-count-cat")?.textContent).toBe("1+");

        release();
        await tick();
        await tick();
        expect(document.getElementById("org-tab-count-cat")?.textContent).toBe("2");
    });

    test("select-all loads the rest and selects every row", async () => {
        fakeHtmx({ "/rows/tag/?after_id=2&all=1": rest });
        mountPage(firstPage, "");

        document.getElementById("org-header-sel-all")!.click();
        await tick();

        expect(selected()).toEqual(["1", "2", "3", "4"]);
        expect(document.getElementById("org-bulk-count")?.textContent).toBe("4 selected");
    });

    test("selecting every loaded row is not selecting every row", () => {
        fakeHtmx({});
        mountPage(firstPage, "");

        document.querySelector<HTMLInputElement>('.tag-select-cb[data-tag-id="1"]')!.click();
        document.querySelector<HTMLInputElement>('.tag-select-cb[data-tag-id="2"]')!.click();

        expect(document.getElementById("org-header-sel-all")?.title).toBe("Select all");
    });

    test("a page arriving keeps the selection, and a re-render clears it", () => {
        mountPage(firstPage, "");
        document.querySelector<HTMLInputElement>('.tag-select-cb[data-tag-id="1"]')!.click();
        const more = document.querySelector<HTMLElement>("#tag-rows .organize-rows-more")!;

        swapLikeHtmx(more, more, "outerHTML", rest);
        expect(selected()).toEqual(["1"]);

        const rows = document.getElementById("tag-rows")!;
        swapLikeHtmx(rows, rows, "innerHTML", firstPage);
        expect(selected()).toEqual([]);
    });

    test("a bulk write asks the server to re-render the rows the user had", async () => {
        mountPage(firstPage, "");
        document.querySelector<HTMLInputElement>('.tag-select-cb[data-tag-id="1"]')!.click();

        window._orgBulk.del!();
        await tick();

        expect(posts.map((p) => [p.url, p.headers[ROWS_LOADED_HEADER]])).toEqual([["/tag/bulk-delete/", "2"]]);
    });

    test("a bulk edit started from Display Order loads the cards it reads", async () => {
        const calls = fakeHtmx({ "/rows/tag/?after_id=2&all=1": rest });
        mountPage(firstPage, "");

        window._orgBulkEditByIds.tag!(["1", "4"]);
        await tick();

        expect(calls).toHaveLength(1);
        expect((document.getElementById("tag-bulk-edit-dialog") as HTMLDialogElement).open).toBe(true);
        expect(document.getElementById("tag-bulk-edit-title")?.textContent).toBe("Edit 2 Tags");
    });

    test("the tree view loads every row, so a child is not shown as a root", async () => {
        const calls = fakeHtmx({ "/rows/tag/?after_id=2&all=1": card("tag", 3, "child", "1") });
        localStorage.setItem("organize_view", "tree");
        mountPage(firstPage, "");
        await tick();

        expect(calls).toHaveLength(1);
        const roots = Array.from(document.querySelectorAll<HTMLElement>('#tag-rows .tag-tree-root > .tag-tree-item[data-depth="0"] > .tag-card')).map((c) => c.dataset.tagId);
        expect(roots).toEqual(["1", "2"]);
        localStorage.removeItem("organize_view");
    });

    test("a filter pass over another tab leaves the select-all button to the tab on screen", () => {
        mountPage(card("tag", 1, "alpha"), card("category", 9, "elsewhere"));
        document.querySelector<HTMLInputElement>('.tag-select-cb[data-tag-id="1"]')!.click();
        expect(document.getElementById("org-header-sel-all")?.title).toBe("Deselect all");

        applyOrgFilter("cat");

        expect(document.getElementById("org-header-sel-all")?.title).toBe("Deselect all");
    });
});
