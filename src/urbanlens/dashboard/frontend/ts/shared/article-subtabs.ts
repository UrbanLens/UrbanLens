/**
 * The Article tab's sub-tab strip on the pin and wiki pages. Every panel stays in the DOM, so switching only
 * toggles visibility and nothing is re-fetched.
 */

function select(tab: HTMLElement): void {
    const key = tab.dataset.articleSubtab ?? "";
    const root: ParentNode = tab.closest("[data-tab-panel]") ?? document;
    root.querySelectorAll<HTMLElement>('.article-subtabs [role="tab"]').forEach((other) => {
        const active = other === tab;
        other.classList.toggle("active", active);
        other.setAttribute("aria-selected", active ? "true" : "false");
    });
    root.querySelectorAll<HTMLElement>('[data-article-subtab]:not([role="tab"])').forEach((panel) => {
        panel.hidden = panel.dataset.articleSubtab !== key;
    });
    window.ulRefreshCollapseRestore?.();
}

function onClick(event: MouseEvent): void {
    const tab = event.target instanceof Element ? event.target.closest<HTMLElement>('.article-subtabs [role="tab"][data-article-subtab]') : null;
    if (tab) select(tab);
}

export function installGlobalArticleSubtabs(): void {
    document.addEventListener("click", onClick);
}
