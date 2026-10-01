import { beforeEach, expect, test } from "bun:test";

import { installActionsFab } from "./actions-fab";

const FAB = `
  <div class="pin-actions-fab" id="pin-actions-fab" data-user="7">
    <button type="button" id="pin-actions-collapse">collapse</button>
    <div id="pin-actions-menu">
      <button type="button" id="pin-actions-undo" disabled title="Undo" data-tooltip="Undo">undo</button>
      <button type="button" data-article-page-action hidden>Source</button>
      <button type="button" id="pin-actions-hidden-sections">hidden</button>
    </div>
    <button type="button" id="pin-actions-fab-btn" hidden>Actions</button>
  </div>
  <div data-page-tabs><button data-tab="overview" class="is-active">Overview</button><button data-tab="article">Article</button></div>
  <button type="button" id="ul-undo-btn" hidden aria-label="Undo">u</button>
  <button type="button" id="tools-fab-btn">tools</button>`;

const $ = (id: string) => document.getElementById(id);
let uninstall: (() => void) | null = null;
const install = () => {
    uninstall?.();
    const fab = $("pin-actions-fab");
    if (!fab) throw new Error("no fab");
    uninstall = installActionsFab(fab);
};

beforeEach(() => {
    localStorage.clear();
    document.body.innerHTML = FAB;
});

test("collapsing is remembered per user", () => {
    install();
    expect($("pin-actions-menu")?.hidden).toBe(false);
    $("pin-actions-collapse")?.click();
    expect([$("pin-actions-menu")?.hidden, $("pin-actions-collapse")?.hidden, $("pin-actions-fab-btn")?.hidden]).toEqual([true, true, false]);
    expect($("pin-actions-fab")?.classList.contains("is-collapsed")).toBe(true);
    expect(localStorage.getItem("ul-pin-actions-expanded:7")).toBe("0");

    document.body.innerHTML = FAB;
    install();
    expect($("pin-actions-menu")?.hidden).toBe(true);
    $("pin-actions-fab-btn")?.click();
    expect($("pin-actions-menu")?.hidden).toBe(false);
    expect(localStorage.getItem("ul-pin-actions-expanded:7")).toBe("1");
});

test("undo mirrors the undo bar, from its current state and from each change", () => {
    const source = $("ul-undo-btn");
    if (source) {
        source.hidden = false;
        source.setAttribute("aria-label", "Undo: Rename pin");
    }
    install();
    const undo = $("pin-actions-undo");
    expect(undo instanceof HTMLButtonElement && undo.disabled).toBe(false);
    expect(undo?.title).toBe("Undo: Rename pin");

    document.dispatchEvent(new CustomEvent("ul:undo-state", { detail: { canUndo: false, label: "Undo" } }));
    expect(undo instanceof HTMLButtonElement && undo.disabled).toBe(true);
    expect(undo?.getAttribute("data-tooltip")).toBe("Undo");

    document.dispatchEvent(new CustomEvent("ul:undo-state", { detail: { canUndo: true, label: "Undo: Move pin" } }));
    const clicks: string[] = [];
    source?.addEventListener("click", () => clicks.push("undo"));
    undo?.click();
    expect(clicks).toEqual(["undo"]);
    expect(undo?.title).toBe("Undo: Move pin");
});

test("hidden sections opens the tools panel, and article actions follow the article tab", () => {
    install();
    const opened: string[] = [];
    $("tools-fab-btn")?.addEventListener("click", () => opened.push("tools"));
    $("pin-actions-hidden-sections")?.click();
    expect(opened).toEqual(["tools"]);

    const action = document.querySelector<HTMLElement>("[data-article-page-action]");
    expect(action?.hidden).toBe(true);
    document.body.dispatchEvent(new CustomEvent("ul:tabShown", { bubbles: true, detail: { tab: "article" } }));
    expect(action?.hidden).toBe(false);
    document.body.dispatchEvent(new CustomEvent("ul:tabShown", { bubbles: true, detail: { tab: "overview" } }));
    expect(action?.hidden).toBe(true);
});
