import { beforeAll, describe, expect, test } from "bun:test";

import { installGlobalArticleSubtabs } from "./article-subtabs";

describe("article sub-tabs", () => {
    beforeAll(() => installGlobalArticleSubtabs());

    function mount(): void {
        document.body.innerHTML = `
          <section data-tab-panel="article">
            <div class="card-tabs article-subtabs" role="tablist">
              <button type="button" role="tab" class="active" aria-selected="true" data-article-subtab="article"><span id="a-label">Article</span></button>
              <button type="button" role="tab" aria-selected="false" data-article-subtab="links">Links</button>
            </div>
            <div data-article-subtab="article">A</div>
            <div data-article-subtab="links" hidden>L</div>
          </section>
          <section data-tab-panel="other"><div data-article-subtab="links">elsewhere</div></section>`;
    }

    function panel(key: string): HTMLElement {
        return document.querySelector<HTMLElement>(`section[data-tab-panel="article"] div[data-article-subtab="${key}"]`) as HTMLElement;
    }

    test("a tab shows its own panel and hides the others, within its tab panel only", () => {
        mount();
        document.querySelector<HTMLElement>('[role="tab"][data-article-subtab="links"]')?.dispatchEvent(new MouseEvent("click", { bubbles: true }));

        expect(panel("links").hidden).toBe(false);
        expect(panel("article").hidden).toBe(true);
        expect(document.querySelector<HTMLElement>('section[data-tab-panel="other"] div')?.hidden).toBe(false);
        expect(document.querySelector('[role="tab"][data-article-subtab="links"]')?.getAttribute("aria-selected")).toBe("true");
        expect(document.querySelector('[role="tab"][data-article-subtab="article"]')?.classList.contains("active")).toBe(false);
    });

    test("a click on the tab's label counts as a click on the tab", () => {
        mount();
        document.querySelector<HTMLElement>('[role="tab"][data-article-subtab="links"]')?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        document.getElementById("a-label")?.dispatchEvent(new MouseEvent("click", { bubbles: true }));

        expect(panel("article").hidden).toBe(false);
        expect(panel("links").hidden).toBe(true);
    });
});
