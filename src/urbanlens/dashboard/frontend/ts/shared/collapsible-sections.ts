/**
 * Collapsible page sections, and the tools-FAB list that restores them.
 *
 * Any element with ``[data-collapse-scope][data-collapse-section]`` gets a chevron
 * button injected into its header that toggles an ``.is-collapsed`` class, hiding
 * the whole section, header included. State is persisted in ``localStorage`` keyed
 * by scope ("pin", "wiki", "trip") and section name only - deliberately not by
 * entity id, so collapsing "Comments" on one pin collapses it on every pin,
 * independently of the wiki and trip pages.
 *
 * Because collapsing hides the header too, there is nothing left to click to bring
 * a section back. The fixed ``#tools-fab`` lists the current page's collapsed
 * sections so they can be restored individually.
 *
 * Ported out of ``base.html``'s inline script unchanged. Listeners are attached
 * once the document is ready rather than at import: this ships in the classic
 * ``core.js`` bundle loaded from ``<head>``, where ``document.body`` is still null
 * and none of the sections exist yet.
 */

const forcedOpen = new Set<string>();

function key(scope: string, section: string): string {
    return `ul-collapsed:${scope}:${section}`;
}

function isCollapsed(scope: string, section: string): boolean {
    try {
        return localStorage.getItem(key(scope, section)) === "1";
    } catch {
        return false;
    }
}

function setCollapsed(scope: string, section: string, collapsed: boolean): void {
    try {
        if (collapsed) localStorage.setItem(key(scope, section), "1");
        else localStorage.removeItem(key(scope, section));
    } catch {
        /* storage unavailable (private mode) - the class toggle still works for this page */
    }
}

function applyState(container: HTMLElement): void {
    const scope = container.dataset.collapseScope;
    const section = container.dataset.collapseSection;
    if (!scope || !section) return;

    // data-collapse-if-empty is set server-side whenever a section currently has no
    // content. It collapses by default alongside the stored preference but is never
    // written to localStorage - it is re-derived from the section's actual content on
    // every load rather than being a sticky choice. forcedOpen overrides it for this
    // page load only, so an explicit restore-from-FAB sticks.
    const emptyByDefault = container.dataset.collapseIfEmpty === "true" && !forcedOpen.has(key(scope, section));
    const collapsed = isCollapsed(scope, section) || emptyByDefault;
    container.classList.toggle("is-collapsed", collapsed);
    container.querySelector(".section-collapse-btn")?.setAttribute("aria-expanded", String(!collapsed));
}

function headerOf(container: HTMLElement): HTMLElement | null {
    return container.querySelector(":scope > .card-header, :scope > .wiki-card-header, :scope > .tag-panel-header, :scope > .wiki-tabs");
}

/** The section's display name, read from its header so the restore menu can show
 * something more useful than the section slug. */
function labelOf(container: HTMLElement, header: HTMLElement | null): string {
    if (container.dataset.collapseLabel) return container.dataset.collapseLabel;
    const titleEl = header?.querySelector(":scope > span:not(.badge):not(.visit-count), :scope > h2, :scope > h3, :scope > h4");
    const label = titleEl?.textContent?.trim() || (container.dataset.collapseSection ?? "");
    container.dataset.collapseLabel = label;
    return label;
}

function actionsGroup(header: HTMLElement): HTMLElement {
    const existing = header.querySelector<HTMLElement>(":scope > .card-header-actions");
    if (existing) return existing;

    const group = document.createElement("div");
    group.className = "card-header-actions";
    const toMove = Array.from(header.children).filter(
        (el) => el.matches(".btn--icon-sm, .gallery-upload-compact-btn, .card-header-link, .btn, .section-collapse-btn") && !el.classList.contains("view-toggle-btn"),
    );
    toMove.forEach((el) => group.appendChild(el));
    header.appendChild(group);
    return group;
}

/** Headers can arrive after a container is first seen (HTMX content still loading),
 * so this is idempotent and safe to call repeatedly. */
function ensureToggle(container: HTMLElement): void {
    const header = headerOf(container);
    if (header) labelOf(container, header);
    if (container.dataset.collapseInit === "1") {
        applyState(container);
        return;
    }
    if (!header) return;

    const actions = actionsGroup(header);
    if (!actions.querySelector(".section-collapse-btn")) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn--icon-sm section-collapse-btn";
        btn.setAttribute("aria-label", "Collapse section");
        btn.innerHTML = '<i class="material-symbols-outlined">expand_less</i>';
        actions.appendChild(btn);
    }
    container.dataset.collapseInit = "1";
    applyState(container);
}

/** A page only ever uses one collapse scope, so the single global tools menu can
 * auto-detect the active one instead of every page declaring its own control. */
function currentScope(): string | null {
    return document.querySelector<HTMLElement>("[data-collapse-scope]")?.dataset.collapseScope ?? null;
}

function closeToolsFab(): void {
    const menu = document.getElementById("tools-fab-menu");
    const btn = document.getElementById("tools-fab-btn");
    if (menu) menu.hidden = true;
    btn?.setAttribute("aria-expanded", "false");
}

export function updateRestoreControls(): void {
    const fab = document.getElementById("tools-fab");
    const group = fab?.querySelector<HTMLElement>('[data-tools-fab-group="collapse-restore"]');
    if (!fab || !group) return;

    const scope = currentScope();
    // Detail pages render every tab's markup at once and only toggle `hidden`, so a
    // section collapsed on one tab would otherwise be offered while viewing another.
    // .closest('[hidden]') excludes anything inside a hidden tab wrapper at any depth.
    const hiddenSections = scope
        ? Array.from(document.querySelectorAll<HTMLElement>(`[data-collapse-scope="${scope}"][data-collapse-section].is-collapsed`)).filter((section) => !section.closest("[hidden]"))
        : [];

    const restoreMenu = group.querySelector<HTMLElement>(".collapse-restore-menu");
    const countEl = fab.querySelector<HTMLElement>(".collapse-restore-count");

    group.hidden = hiddenSections.length === 0;
    fab.hidden = hiddenSections.length === 0;
    if (countEl) {
        countEl.hidden = hiddenSections.length === 0;
        countEl.textContent = String(hiddenSections.length);
    }
    if (!hiddenSections.length) {
        closeToolsFab();
        if (restoreMenu) restoreMenu.innerHTML = "";
        return;
    }
    if (!restoreMenu) return;

    restoreMenu.innerHTML = "";
    for (const section of hiddenSections) {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "collapse-restore-item";
        item.innerHTML = '<i class="material-symbols-outlined">visibility</i>';
        item.append(section.dataset.collapseLabel || (section.dataset.collapseSection ?? ""));
        item.addEventListener("click", () => {
            const sectionName = section.dataset.collapseSection ?? "";
            section.classList.remove("is-collapsed");
            section.querySelector(".section-collapse-btn")?.setAttribute("aria-expanded", "true");
            if (scope) {
                setCollapsed(scope, sectionName, false);
                forcedOpen.add(key(scope, sectionName));
            }
            // The section was hidden before its hx-get ever fired, so its content was
            // never loaded - kick off that request now.
            if (section.getAttribute("hx-get") && window.htmx) window.htmx.trigger(section, "ul:unhide");
            const innerLoader = section.querySelector('[hx-get][hx-trigger*="ul:unhide"]');
            if (innerLoader && window.htmx) window.htmx.trigger(innerLoader, "ul:unhide");
            updateRestoreControls();
        });
        restoreMenu.appendChild(item);
    }
}

export function scanAll(): void {
    document.querySelectorAll<HTMLElement>("[data-collapse-scope][data-collapse-section]").forEach(ensureToggle);
    updateRestoreControls();
}

function onDocumentClick(event: MouseEvent): void {
    const target = event.target as HTMLElement | null;
    if (!target?.closest) return;

    const fabToggle = target.closest<HTMLElement>("#tools-fab-btn");
    if (fabToggle) {
        const menu = document.getElementById("tools-fab-menu");
        const opening = !!menu?.hidden;
        if (menu) menu.hidden = !opening;
        fabToggle.setAttribute("aria-expanded", String(opening));
        return;
    }

    const btn = target.closest<HTMLElement>(".section-collapse-btn");
    if (btn) {
        const container = btn.closest<HTMLElement>("[data-collapse-scope][data-collapse-section]");
        if (!container) return;
        const collapsed = !container.classList.contains("is-collapsed");
        container.classList.toggle("is-collapsed", collapsed);
        btn.setAttribute("aria-expanded", String(!collapsed));
        setCollapsed(container.dataset.collapseScope ?? "", container.dataset.collapseSection ?? "", collapsed);
        updateRestoreControls();
        return;
    }

    // A click outside the open tools menu closes it.
    const fab = document.getElementById("tools-fab");
    const openMenu = document.getElementById("tools-fab-menu");
    if (fab && openMenu && !openMenu.hidden && !fab.contains(target)) closeToolsFab();
}

/** Reset module state. Test-only: a fresh document invalidates the forced-open set. */
export function resetCollapsibleSectionsForTests(): void {
    forcedOpen.clear();
}

declare global {
    interface Window {
        ulSectionCollapsed?: typeof isCollapsed;
        ulRefreshCollapseRestore?: typeof updateRestoreControls;
    }
}

export function installGlobalCollapsibleSections(): void {
    // Exposed so hx-trigger conditions on lazy-loaded sections can skip firing their
    // request when the section is already hidden.
    window.ulSectionCollapsed = isCollapsed;
    // Exposed for tab switches that do not go through page-tabs.js's `ul:tabShown`
    // (e.g. the Article tab's internal sub-tab toggle).
    window.ulRefreshCollapseRestore = updateRestoreControls;

    document.addEventListener("click", onDocumentClick);
    document.addEventListener("htmx:afterSettle", scanAll);

    // page-tabs.js dispatches ul:tabShown on <body>, which does not exist yet when
    // this runs from the <head> - so both this binding and the first scan wait for
    // the document. The inline version this replaced ran after <body> and could bind
    // immediately.
    const ready = (): void => {
        document.body.addEventListener("ul:tabShown", updateRestoreControls);
        scanAll();
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready);
    else ready();
}
