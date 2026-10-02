import { beforeAll, describe, expect, test } from "bun:test";

import { installCardTabs } from "./card-tabs";

beforeAll(() => installCardTabs());

function click(el: Element | null): void {
    el?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
}

function tab(selector: string): HTMLElement {
    return document.querySelector<HTMLElement>(selector) as HTMLElement;
}

describe("card tabs", () => {
    test("marks the clicked tab active and selected, and clears the rest of its strip", () => {
        document.body.innerHTML = `
          <div data-card-tabs>
            <button id="a" data-card-tab class="active" role="tab" aria-selected="true">A</button>
            <button id="b" data-card-tab role="tab" aria-selected="false"><span id="b-label">B</span></button>
          </div>`;
        click(document.getElementById("b-label"));

        expect(tab("#b").classList.contains("active")).toBe(true);
        expect(tab("#b").getAttribute("aria-selected")).toBe("true");
        expect(tab("#a").classList.contains("active")).toBe(false);
        expect(tab("#a").getAttribute("aria-selected")).toBe("false");
    });

    test("uses the active class the group names", () => {
        document.body.innerHTML = `
          <div data-card-tabs="is-active">
            <button id="a" data-card-tab class="is-active">A</button>
            <button id="b" data-card-tab>B</button>
          </div>`;
        click(tab("#b"));

        expect(tab("#b").classList.contains("is-active")).toBe(true);
        expect(tab("#a").classList.contains("is-active")).toBe(false);
        expect(tab("#b").classList.contains("active")).toBe(false);
    });

    test("a tab naming a pane shows only that pane of its group", () => {
        document.body.innerHTML = `
          <div data-card-tabs>
            <button id="a" data-card-tab="one" class="active">A</button>
            <button id="b" data-card-tab="two">B</button>
            <div id="one" data-card-pane="one"></div>
            <div id="two" data-card-pane="two" hidden></div>
          </div>
          <div id="elsewhere" data-card-pane="one" hidden></div>`;
        click(tab("#b"));

        expect(tab("#one").hidden).toBe(true);
        expect(tab("#two").hidden).toBe(false);
        expect(tab("#elsewhere").hidden).toBe(true);

        click(tab("#a"));
        expect(tab("#one").hidden).toBe(false);
        expect(tab("#two").hidden).toBe(true);
    });

    test("a tab without a pane leaves the group's panes alone", () => {
        document.body.innerHTML = `
          <div data-card-tabs>
            <button id="a" data-card-tab class="active">A</button>
            <div id="pane" data-card-pane="x" hidden></div>
          </div>`;
        click(tab("#a"));

        expect(tab("#pane").hidden).toBe(true);
    });

    test("a group nested in another's pane is independent of it", () => {
        document.body.innerHTML = `
          <div data-card-tabs>
            <button id="outer-a" data-card-tab class="active">Outer A</button>
            <button id="outer-b" data-card-tab>Outer B</button>
            <div data-card-tabs>
              <button id="inner-a" data-card-tab="x" class="active">Inner A</button>
              <button id="inner-b" data-card-tab="y">Inner B</button>
              <div id="x" data-card-pane="x"></div>
              <div id="y" data-card-pane="y" hidden></div>
            </div>
          </div>`;
        click(tab("#inner-b"));

        expect(tab("#outer-a").classList.contains("active")).toBe(true);
        expect(tab("#inner-b").classList.contains("active")).toBe(true);
        expect(tab("#y").hidden).toBe(false);

        click(tab("#outer-b"));
        expect(tab("#inner-b").classList.contains("active")).toBe(true);
        expect(tab("#y").hidden).toBe(false);
    });

    test("a programmatic click() switches tabs too", () => {
        document.body.innerHTML = `
          <div data-card-tabs>
            <button id="a" data-card-tab class="active">A</button>
            <button id="b" data-card-tab>B</button>
          </div>`;
        tab("#b").click();

        expect(tab("#b").classList.contains("active")).toBe(true);
    });
});
