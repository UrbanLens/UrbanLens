/**
 * Behavioural tests for collapsible sections, against a real document.
 *
 * This logic spent its life as inline template JS where none of it could be
 * asserted - the persistence keying, the chevron injection, the restore list's
 * hidden-tab filtering. These cover the parts that decide what a user sees.
 */

import { beforeEach, describe, expect, mock, test } from "bun:test";

import { installGlobalCollapsibleSections, resetCollapsibleSectionsForTests, scanAll, updateRestoreControls } from "./collapsible-sections";

const SECTION = (scope: string, name: string, extra = "") => `
  <div class="card" data-collapse-scope="${scope}" data-collapse-section="${name}" ${extra}>
    <div class="card-header"><h2>${name} heading</h2></div>
    <div class="card__body">content</div>
  </div>`;

const FAB = `
  <div id="tools-fab" hidden>
    <button id="tools-fab-btn" aria-expanded="false"><span class="collapse-restore-count" hidden></span></button>
    <div id="tools-fab-menu" hidden>
      <div data-tools-fab-group="collapse-restore" hidden><div class="collapse-restore-menu"></div></div>
    </div>
  </div>`;

function section(name: string): HTMLElement {
    return document.querySelector<HTMLElement>(`[data-collapse-section="${name}"]`)!;
}

function clickToggle(name: string): void {
    section(name).querySelector(".section-collapse-btn")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
}

beforeEach(() => {
    localStorage.clear();
    resetCollapsibleSectionsForTests();
    document.body.innerHTML = FAB + SECTION("pin", "comments") + SECTION("pin", "photos");
    installGlobalCollapsibleSections();
});

describe("chevron injection", () => {
    test("every scoped section gets exactly one toggle, however often it is scanned", () => {
        scanAll();
        scanAll();
        expect(section("comments").querySelectorAll(".section-collapse-btn")).toHaveLength(1);
    });

    test("a section with no header is skipped rather than throwing", () => {
        document.body.innerHTML = '<div data-collapse-scope="pin" data-collapse-section="headerless"></div>';
        expect(() => scanAll()).not.toThrow();
    });
});

describe("collapsing", () => {
    test("clicking the toggle collapses the section and records it", () => {
        scanAll();
        clickToggle("comments");

        expect(section("comments").classList.contains("is-collapsed")).toBe(true);
        expect(localStorage.getItem("ul-collapsed:pin:comments")).toBe("1");
        expect(section("comments").querySelector(".section-collapse-btn")?.getAttribute("aria-expanded")).toBe("false");
    });

    test("clicking again expands it and clears the record", () => {
        scanAll();
        clickToggle("comments");
        clickToggle("comments");

        expect(section("comments").classList.contains("is-collapsed")).toBe(false);
        expect(localStorage.getItem("ul-collapsed:pin:comments")).toBeNull();
    });

    test("state is keyed by scope and section, not by entity - so it applies across pages", () => {
        localStorage.setItem("ul-collapsed:pin:comments", "1");
        scanAll();

        expect(section("comments").classList.contains("is-collapsed")).toBe(true);
        expect(section("photos").classList.contains("is-collapsed")).toBe(false);
    });

    test("collapsing one scope leaves another scope's identically-named section alone", () => {
        document.body.innerHTML = FAB + SECTION("pin", "comments") + SECTION("wiki", "comments");
        scanAll();
        localStorage.setItem("ul-collapsed:pin:comments", "1");
        scanAll();

        const sections = Array.from(document.querySelectorAll<HTMLElement>('[data-collapse-section="comments"]'));
        expect(sections).toHaveLength(2);
        expect(sections[0]?.classList.contains("is-collapsed")).toBe(true);
        expect(sections[1]?.classList.contains("is-collapsed")).toBe(false);
    });
});

describe("collapse-if-empty", () => {
    test("an empty section collapses by default without being persisted", () => {
        document.body.innerHTML = FAB + SECTION("pin", "links", 'data-collapse-if-empty="true"');
        scanAll();

        expect(section("links").classList.contains("is-collapsed")).toBe(true);
        // Never written: it is re-derived from content each load, not a sticky choice.
        expect(localStorage.getItem("ul-collapsed:pin:links")).toBeNull();
    });

    test("restoring it from the FAB sticks for the rest of the page load", () => {
        document.body.innerHTML = FAB + SECTION("pin", "links", 'data-collapse-if-empty="true"');
        scanAll();
        document.querySelector<HTMLElement>(".collapse-restore-item")!.dispatchEvent(new Event("click"));

        expect(section("links").classList.contains("is-collapsed")).toBe(false);
        // A re-scan must not immediately re-collapse it, which would flash it away.
        scanAll();
        expect(section("links").classList.contains("is-collapsed")).toBe(false);
    });
});

describe("the restore list", () => {
    test("stays hidden while nothing is collapsed", () => {
        scanAll();
        expect(document.getElementById("tools-fab")?.hidden).toBe(true);
    });

    test("appears with a count once a section is collapsed", () => {
        scanAll();
        clickToggle("comments");

        expect(document.getElementById("tools-fab")?.hidden).toBe(false);
        expect(document.querySelector(".collapse-restore-count")?.textContent).toBe("1");
        expect(document.querySelectorAll(".collapse-restore-item")).toHaveLength(1);
    });

    test("names sections by their heading rather than their slug", () => {
        scanAll();
        clickToggle("comments");
        expect(document.querySelector(".collapse-restore-item")?.textContent).toContain("comments heading");
    });

    test("omits sections inside a hidden tab, at any depth", () => {
        // Detail pages render every tab at once and only toggle `hidden`, so a section
        // collapsed on another tab must not be offered while viewing this one.
        document.body.innerHTML = FAB + SECTION("pin", "comments") + `<div hidden><div>${SECTION("pin", "buried")}</div></div>`;
        scanAll();
        clickToggle("comments");
        section("buried").classList.add("is-collapsed");
        updateRestoreControls();

        const labels = Array.from(document.querySelectorAll(".collapse-restore-item")).map((el) => el.textContent);
        expect(labels).toHaveLength(1);
        expect(labels[0]).toContain("comments");
    });

    test("restoring fires the section's deferred hx-get", () => {
        const trigger = mock((_el: Element, _event: string) => {});
        window.htmx = { process: () => {}, trigger } as unknown as typeof window.htmx;

        document.body.innerHTML = FAB + SECTION("pin", "lazy", 'hx-get="/lazy/"');
        scanAll();
        clickToggle("lazy");
        document.querySelector<HTMLElement>(".collapse-restore-item")!.dispatchEvent(new Event("click"));

        expect(trigger).toHaveBeenCalled();
        expect(trigger.mock.calls[0]?.[1]).toBe("ul:unhide");
    });

    test("empties itself again when the last section is restored", () => {
        scanAll();
        clickToggle("comments");
        document.querySelector<HTMLElement>(".collapse-restore-item")!.dispatchEvent(new Event("click"));

        expect(document.getElementById("tools-fab")?.hidden).toBe(true);
        expect(document.querySelectorAll(".collapse-restore-item")).toHaveLength(0);
    });
});

describe("the tools FAB menu", () => {
    test("the button opens and closes it", () => {
        scanAll();
        clickToggle("comments");
        const btn = document.getElementById("tools-fab-btn")!;

        btn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect(document.getElementById("tools-fab-menu")?.hidden).toBe(false);

        btn.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect(document.getElementById("tools-fab-menu")?.hidden).toBe(true);
    });

    test("a click outside closes it", () => {
        scanAll();
        clickToggle("comments");
        document.getElementById("tools-fab-btn")!.dispatchEvent(new MouseEvent("click", { bubbles: true }));

        document.body.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        expect(document.getElementById("tools-fab-menu")?.hidden).toBe(true);
    });
});

describe("installGlobalCollapsibleSections", () => {
    test("exposes the two globals templates and hx-triggers rely on", () => {
        expect(typeof window.ulSectionCollapsed).toBe("function");
        expect(typeof window.ulRefreshCollapseRestore).toBe("function");
    });

    test("ulSectionCollapsed reports the stored state", () => {
        localStorage.setItem("ul-collapsed:pin:comments", "1");
        expect(window.ulSectionCollapsed?.("pin", "comments")).toBe(true);
        expect(window.ulSectionCollapsed?.("pin", "photos")).toBe(false);
    });
});
