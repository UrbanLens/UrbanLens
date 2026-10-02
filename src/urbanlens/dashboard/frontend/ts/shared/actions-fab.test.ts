import { beforeEach, describe, expect, test } from "bun:test";

import { installActionsFab } from "./actions-fab";
import { installGlobalCollapsibleSections, resetCollapsibleSectionsForTests, scanAll } from "./collapsible-sections";

const FAB = `
  <div class="pin-actions-fab map-buttons" id="pin-actions-fab" data-user="7">
    <button type="button" class="map-toolbar-collapse" id="pin-actions-collapse">collapse</button>
    <div class="map-buttons-content" id="pin-actions-menu">
      <button type="button" id="pin-actions-undo" disabled title="Undo" data-tooltip="Undo">undo</button>
      <button type="button" data-article-page-action hidden>Source</button>
      <button type="button" id="pin-actions-hidden-sections" data-opens-tools-fab>hidden</button>
    </div>
    <button type="button" id="pin-actions-fab-btn" hidden>Actions</button>
  </div>
  <div data-page-tabs><button data-tab="overview" class="is-active">Overview</button><button data-tab="article">Article</button></div>
  <button type="button" id="ul-undo-btn" hidden aria-label="Undo">u</button>`;

const TOOLS_FAB = `
  <div id="tools-fab" hidden>
    <button id="tools-fab-btn" aria-expanded="false"><span class="collapse-restore-count" hidden></span></button>
    <div id="tools-fab-menu" hidden>
      <div data-tools-fab-group="collapse-restore" hidden><div class="collapse-restore-menu"></div></div>
    </div>
  </div>
  <div class="card" data-collapse-scope="pin" data-collapse-section="links">
    <div class="card-header"><h2>Links</h2></div><div class="card__body">content</div>
  </div>`;

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
    resetCollapsibleSectionsForTests();
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

test("article actions follow the article tab", () => {
    install();
    const action = document.querySelector<HTMLElement>("[data-article-page-action]");
    expect(action?.hidden).toBe(true);
    document.body.dispatchEvent(new CustomEvent("ul:tabShown", { bubbles: true, detail: { tab: "article" } }));
    expect(action?.hidden).toBe(false);
    document.body.dispatchEvent(new CustomEvent("ul:tabShown", { bubbles: true, detail: { tab: "overview" } }));
    expect(action?.hidden).toBe(true);
});

test("a page-wide toolbar collapse handler never desyncs the toolbar across close and reopen", () => {
    // comment-map.js toggles `collapsed` on every .map-buttons whose .map-toolbar-collapse is clicked, which left the
    // reopened menu at zero width on every other cycle.
    const pageWide = (event: Event): void => {
        const toggle = event.target instanceof Element ? event.target.closest(".map-toolbar-collapse") : null;
        toggle?.closest(".map-buttons")?.classList.toggle("collapsed");
    };
    document.addEventListener("click", pageWide);
    try {
        install();
        for (let cycle = 0; cycle < 3; cycle++) {
            $("pin-actions-collapse")?.click();
            $("pin-actions-fab-btn")?.click();
            expect($("pin-actions-fab")?.classList.contains("collapsed")).toBe(false);
            expect($("pin-actions-menu")?.hidden).toBe(false);
        }
    } finally {
        document.removeEventListener("click", pageWide);
    }
});

describe("hidden sections", () => {
    const hideLinks = (): void => {
        document.querySelector('[data-collapse-section="links"] .section-collapse-btn')?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    };
    const setUp = (): void => {
        document.body.innerHTML = FAB + TOOLS_FAB;
        installGlobalCollapsibleSections();
        install();
        scanAll();
    };

    test("is offered only while a section is hidden", () => {
        setUp();
        expect($("pin-actions-hidden-sections")?.hidden).toBe(true);
        hideLinks();
        expect($("pin-actions-hidden-sections")?.hidden).toBe(false);
    });

    test("opens the tools menu listing what is hidden, and a second click closes it", () => {
        setUp();
        hideLinks();
        $("pin-actions-hidden-sections")?.click();
        expect($("tools-fab-menu")?.hidden).toBe(false);
        const items = document.querySelectorAll(".collapse-restore-item");
        expect(items).toHaveLength(1);
        expect(items[0]?.textContent).toContain("Links");
        $("pin-actions-hidden-sections")?.click();
        expect($("tools-fab-menu")?.hidden).toBe(true);
    });
});
