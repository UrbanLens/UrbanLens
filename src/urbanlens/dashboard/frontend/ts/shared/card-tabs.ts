/**
 * Tab strips and view toggles declared in markup. ``data-card-tabs`` marks the group, its value the active class
 * (default ``active``); each ``data-card-tab`` is a tab, ``aria-selected`` kept on those with ``role="tab"``. A tab
 * whose value names a pane shows the group's matching ``data-card-pane`` and hides the others. A tab's content can
 * still come from its own ``hx-get``.
 */

function ownedBy(group: Element, selector: string): HTMLElement[] {
    return Array.from(group.querySelectorAll<HTMLElement>(selector)).filter((el) => el.parentElement?.closest("[data-card-tabs]") === group);
}

function select(tab: HTMLElement): void {
    const group = tab.closest("[data-card-tabs]");
    if (!group) return;
    const activeClass = group.getAttribute("data-card-tabs") || "active";
    for (const other of ownedBy(group, "[data-card-tab]")) {
        other.classList.toggle(activeClass, other === tab);
        if (other.getAttribute("role") === "tab") other.setAttribute("aria-selected", other === tab ? "true" : "false");
    }
    const key = tab.dataset.cardTab;
    if (!key) return;
    for (const pane of ownedBy(group, "[data-card-pane]")) pane.hidden = pane.dataset.cardPane !== key;
}

function onClick(event: Event): void {
    const tab = event.target instanceof Element ? event.target.closest<HTMLElement>("[data-card-tab]") : null;
    if (tab) select(tab);
}

let installed = false;

export function installCardTabs(): void {
    if (installed) return;
    installed = true;
    document.addEventListener("click", onClick);
}
