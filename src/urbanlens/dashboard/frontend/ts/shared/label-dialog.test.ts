import { beforeAll, beforeEach, expect, test } from "bun:test";

import { installLabelDialogs } from "./label-dialog";

const DIALOG = `
  <dialog id="dlg" class="tag-add-dialog">
    <div class="tad-top-tabs">
      <button type="button" class="tad-top-tab tad-top-tab--active" data-top-tab="labels">Labels</button>
      <button type="button" class="tad-top-tab" data-top-tab="lists">Lists</button>
    </div>
    <div class="tad-top-panel" data-top-panel="labels">
      <div class="tad-tabs">
        <button class="tad-tab tad-tab--active" data-tab="all">All</button>
        <button class="tad-tab" data-tab="tag">Tags</button>
        <button class="tad-tab" data-tab="status">Statuses</button>
      </div>
      <input type="search" class="dialog-search">
      <ul>
        <li class="tag-dialog-item" data-name="rust" data-kind="tag"><form id="add-rust"><span class="tad-kind-pill"></span></form></li>
        <li class="tag-dialog-item" data-name="rusty gate" data-kind="status"><span class="tad-kind-pill"></span></li>
        <li class="tag-dialog-item" data-name="roof" data-kind="tag"><span class="tad-kind-pill"></span></li>
      </ul>
      <form class="tad-create-row" hidden><input type="hidden" class="tad-create-name"><span class="tad-create-label"></span></form>
    </div>
    <div class="tad-top-panel" data-top-panel="lists" hidden></div>
  </dialog>
  <div class="tad-tabs" id="elsewhere"><button class="tad-tab tad-tab--active" data-tab="all">All</button><button class="tad-tab" data-tab="tag">Tags</button></div>`;

const $ = <T extends Element = HTMLElement>(selector: string): T | null => document.querySelector<T>(selector);
const visible = () => Array.from(document.querySelectorAll<HTMLElement>(".tag-dialog-item")).filter((li) => !li.hidden).map((li) => li.dataset.name);
const search = (value: string) => {
    const input = $<HTMLInputElement>(".dialog-search");
    if (!input) throw new Error("no search");
    input.value = value;
    input.dispatchEvent(new Event("input", { bubbles: true }));
};
const htmx = (target: Element | null, name: string, detail: object = {}) => target?.dispatchEvent(new CustomEvent(name, { bubbles: true, detail }));

beforeAll(() => {
    HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
        this.setAttribute("open", "");
    };
    installLabelDialogs();
});

beforeEach(() => {
    document.body.innerHTML = DIALOG;
});

test("the search and the kind tabs filter together, and the kind pills show only under All", () => {
    search("rus");
    expect(visible()).toEqual(["rust", "rusty gate"]);
    $(".tad-tab[data-tab='tag']")?.click();
    expect(visible()).toEqual(["rust"]);
    expect($(".tad-tab[data-tab='tag']")?.classList.contains("tad-tab--active")).toBe(true);
    expect($(".tad-tab[data-tab='all']")?.classList.contains("tad-tab--active")).toBe(false);
    expect(Array.from(document.querySelectorAll<HTMLElement>(".tad-kind-pill")).every((p) => p.hidden)).toBe(true);
    $(".tad-tab[data-tab='all']")?.click();
    expect(Array.from(document.querySelectorAll<HTMLElement>(".tad-kind-pill")).some((p) => p.hidden)).toBe(false);
});

test("a search with no exact match offers to create it, as typed", () => {
    const row = $(".tad-create-row");
    search("  Rusty Door ");
    expect(row?.hidden).toBe(false);
    expect($<HTMLInputElement>(".tad-create-name")?.value).toBe("Rusty Door");
    expect($(".tad-create-label")?.textContent).toBe("Rusty Door");
    search("roof");
    expect(row?.hidden).toBe(true);
    search("");
    expect(row?.hidden).toBe(true);
    expect(visible()).toEqual(["rust", "rusty gate", "roof"]);
});

test("the top tabs switch panels", () => {
    $(".tad-top-tab[data-top-tab='lists']")?.click();
    expect($("[data-top-panel='labels']")?.hidden).toBe(true);
    expect($("[data-top-panel='lists']")?.hidden).toBe(false);
    expect($(".tad-top-tab[data-top-tab='lists']")?.classList.contains("tad-top-tab--active")).toBe(true);
    expect($(".tad-top-tab[data-top-tab='labels']")?.classList.contains("tad-top-tab--active")).toBe(false);
});

test("kind tabs outside a label dialog are left to their owner", () => {
    $("#elsewhere .tad-tab[data-tab='tag']")?.click();
    expect($("#elsewhere .tad-tab[data-tab='all']")?.classList.contains("tad-tab--active")).toBe(true);
});

test("adding a label from the open dialog reopens its replacement after the swap", () => {
    $<HTMLDialogElement>("#dlg")?.showModal();
    htmx($("#add-rust"), "htmx:beforeRequest");
    document.body.innerHTML = DIALOG;
    htmx(document.body, "htmx:afterSwap");
    expect($<HTMLDialogElement>("#dlg")?.open).toBe(true);
});

test("a refused add leaves no reopen pending for a later unrelated swap", () => {
    const dlg = $<HTMLDialogElement>("#dlg");
    dlg?.showModal();
    htmx($("#add-rust"), "htmx:beforeRequest");
    htmx($("#add-rust"), "htmx:afterRequest", { successful: false });
    dlg?.close();
    htmx(document.body, "htmx:afterSwap");
    expect(dlg?.open).toBe(false);
});

test("a request from a closed dialog does not reopen it", () => {
    htmx($("#add-rust"), "htmx:beforeRequest");
    htmx(document.body, "htmx:afterSwap");
    expect($<HTMLDialogElement>("#dlg")?.open).toBe(false);
});
