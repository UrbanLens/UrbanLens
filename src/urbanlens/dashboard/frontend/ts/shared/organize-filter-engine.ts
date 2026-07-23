export type OrgNamespace = "tag" | "cat" | "status" | "people";

export const ORG_FILTER_NAMESPACES: OrgNamespace[] = ["tag", "cat", "status", "people"];

const NS_LABELS: Record<OrgNamespace, string> = { tag: "tags", cat: "categories", status: "statuses", people: "people" };

const NS_CONFIG: Record<OrgNamespace, { rowsId: string; cardSel: string; idKey: string; nameKey: string; iconKey: string; customIconKey?: string; colorKey: string; parentsKey: string }> = {
    tag: { rowsId: "tag-rows", cardSel: ".tag-card[data-tag-id]", idKey: "tagId", nameKey: "tagName", iconKey: "tagIcon", customIconKey: "tagCustomIcon", colorKey: "tagColor", parentsKey: "tagParents" },
    cat: { rowsId: "category-rows", cardSel: ".tag-card[data-category-id]", idKey: "categoryId", nameKey: "categoryName", iconKey: "categoryIcon", customIconKey: "categoryCustomIcon", colorKey: "categoryColor", parentsKey: "categoryParents" },
    status: { rowsId: "status-rows", cardSel: ".tag-card[data-status-id]", idKey: "statusId", nameKey: "statusName", iconKey: "statusIcon", customIconKey: "statusCustomIcon", colorKey: "statusColor", parentsKey: "statusParents" },
    people: { rowsId: "people-label-rows", cardSel: ".tag-card[data-people-id]", idKey: "peopleId", nameKey: "peopleName", iconKey: "peopleIcon", colorKey: "peopleColor", parentsKey: "peopleParents" },
};

interface SharedFilterState {
    search: string;
    chips: Set<string>;
    color: string;
}

function loadSharedFilter(): SharedFilterState {
    const params = new URLSearchParams(window.location.search);
    return {
        search: params.get("filter_search") ?? "",
        chips: new Set((params.get("filter_chips") ?? "").split(",").filter(Boolean)),
        color: (params.get("filter_color") ?? "").toLowerCase(),
    };
}

const sharedFilter = loadSharedFilter();
let filterBarOpen = false;

function saveSharedFilter(): void {
    const params = new URLSearchParams(window.location.search);
    if (sharedFilter.search) params.set("filter_search", sharedFilter.search);
    else params.delete("filter_search");
    const chips = Array.from(sharedFilter.chips);
    if (chips.length > 0) params.set("filter_chips", chips.join(","));
    else params.delete("filter_chips");
    if (sharedFilter.color) params.set("filter_color", sharedFilter.color);
    else params.delete("filter_color");
    const newUrl = window.location.pathname + (params.toString() ? `?${params.toString()}` : "");
    window.history.replaceState({}, "", newUrl);
}

function captureFilterFromBar(ns: OrgNamespace): void {
    const bar = document.getElementById(`${ns}-filter-bar`);
    const si = document.getElementById(`${ns}-filter-search`) as HTMLInputElement | null;
    sharedFilter.search = si ? si.value : "";
    sharedFilter.chips = new Set();
    sharedFilter.color = "";
    if (bar) {
        bar.querySelectorAll<HTMLElement>(".org-filter-chip.active").forEach((c) => {
            if (c.dataset.filter) sharedFilter.chips.add(c.dataset.filter);
        });
        const colorDot = bar.querySelector<HTMLElement>(".org-filter-color-dot.active");
        if (colorDot) sharedFilter.color = (colorDot.dataset.filter ?? "").replace("color:", "").toLowerCase();
    }
    saveSharedFilter();
}

export function syncOrgFilterUI(): void {
    ORG_FILTER_NAMESPACES.forEach((ns) => {
        const bar = document.getElementById(`${ns}-filter-bar`);
        const si = document.getElementById(`${ns}-filter-search`) as HTMLInputElement | null;
        if (si) si.value = sharedFilter.search;
        bar?.querySelectorAll<HTMLElement>(".org-filter-chip, .org-filter-color-dot").forEach((el) => {
            const f = el.dataset.filter;
            if (!f) return;
            if (f.startsWith("color:")) el.classList.toggle("active", f.replace("color:", "").toLowerCase() === sharedFilter.color);
            else el.classList.toggle("active", sharedFilter.chips.has(f));
        });
    });
}

function updateFilterBtn(): void {
    const filterBtn = document.getElementById("org-header-filter-btn");
    if (!filterBtn) return;
    const hasActive = sharedFilter.search.trim() || sharedFilter.chips.size > 0 || sharedFilter.color;
    filterBtn.classList.toggle("has-filter", !!hasActive);
}

function syncFilterBtnActive(): void {
    document.getElementById("org-header-filter-btn")?.classList.toggle("btn--active", !!document.querySelector(".org-filter-bar.open"));
}

export function getOrgVisibleCards(rows: HTMLElement | null, cardSel: string): HTMLElement[] {
    if (!rows) return [];
    const inTreeView = rows.classList.contains("tag-view--tree");
    const cards = Array.from(rows.querySelectorAll<HTMLElement>(inTreeView ? `.tag-tree-root ${cardSel}` : cardSel));
    return cards.filter((c) => {
        if (inTreeView) {
            const treeItem = c.closest<HTMLElement>(".tag-tree-item");
            return !treeItem || treeItem.style.display !== "none";
        }
        return c.style.display !== "none";
    });
}

function applyFilterForNs(ns: OrgNamespace): void {
    const cfg = NS_CONFIG[ns];
    const rows = document.getElementById(cfg.rowsId);
    if (!rows) return;

    const search = sharedFilter.search.toLowerCase().trim();
    const activeChips = sharedFilter.chips;
    const activeColor = sharedFilter.color;

    const inTreeView = rows.classList.contains("tag-view--tree");
    const allCards = Array.from(rows.querySelectorAll<HTMLElement>(inTreeView ? `.tag-tree-root ${cfg.cardSel}` : cfg.cardSel));

    const hasChildrenSet = new Set<string>();
    if (activeChips.has("has-children")) {
        const childSourceCards = inTreeView ? Array.from(rows.querySelectorAll<HTMLElement>(cfg.cardSel)) : allCards;
        childSourceCards.forEach((c) => {
            (c.dataset[cfg.parentsKey] ?? "")
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean)
                .forEach((pid) => hasChildrenSet.add(pid));
        });
    }

    allCards.forEach((card) => {
        const idVal = card.dataset[cfg.idKey];
        const name = (card.dataset[cfg.nameKey] ?? "").toLowerCase();
        const icon = card.dataset[cfg.iconKey] ?? "";
        const customIcon = cfg.customIconKey ? (card.dataset[cfg.customIconKey] ?? "") : "";
        const anyIcon = icon || customIcon;
        const color = (card.dataset[cfg.colorKey] ?? "").toLowerCase();
        const parents = card.dataset[cfg.parentsKey] ?? "";
        const hasParents = parents.split(",").some((p) => p.trim() !== "");

        let show = true;
        if (search && !name.includes(search)) show = false;
        if (activeChips.has("has-icon") && !anyIcon) show = false;
        if (activeChips.has("no-icon") && anyIcon) show = false;
        if (activeChips.has("has-color") && !color) show = false;
        if (activeChips.has("no-color") && color) show = false;
        if (activeChips.has("has-children") && !hasChildrenSet.has(String(idVal))) show = false;
        if (activeChips.has("has-parents") && !hasParents) show = false;
        if (activeColor && color !== activeColor) show = false;

        if (inTreeView) {
            const treeItem = card.closest<HTMLElement>(".tag-tree-item");
            (treeItem ?? card).style.display = show ? "" : "none";
        } else {
            card.style.display = show ? "" : "none";
        }
    });

    document.dispatchEvent(new CustomEvent("org:filter-applied", { detail: { ns } }));
}

export function applyOrgFilter(ns: OrgNamespace): void {
    applyAllOrgFilters(ns);
}

export function applyAllOrgFilters(triggerNs?: OrgNamespace): void {
    if (triggerNs) {
        const si = document.getElementById(`${triggerNs}-filter-search`) as HTMLInputElement | null;
        if (si) {
            sharedFilter.search = si.value;
            saveSharedFilter();
            syncOrgFilterUI();
        }
    }
    ORG_FILTER_NAMESPACES.forEach((ns) => applyFilterForNs(ns));
    updateFilterBtn();
}

function hasAnyOrgFilter(): boolean {
    return !!(sharedFilter.search || sharedFilter.chips.size > 0 || sharedFilter.color);
}

function countVisibleCards(ns: OrgNamespace): number {
    const cfg = NS_CONFIG[ns];
    const rows = document.getElementById(cfg.rowsId);
    if (!rows) return 0;
    const inTreeView = rows.classList.contains("tag-view--tree");
    const scope = inTreeView ? `.tag-tree-root ${cfg.cardSel}` : cfg.cardSel;
    return Array.from(rows.querySelectorAll<HTMLElement>(scope)).filter((c) => c.style.display !== "none").length;
}

function updateCrossTabCounts(): void {
    if (!hasAnyOrgFilter()) {
        ORG_FILTER_NAMESPACES.forEach((ns) => {
            const countEl = document.getElementById(`org-tab-count-${ns}`);
            if (countEl) countEl.hidden = true;
            const footer = document.getElementById(`org-cross-tab-${ns}`);
            if (footer) footer.hidden = true;
        });
        return;
    }

    const counts: Record<OrgNamespace, number> = { tag: 0, cat: 0, status: 0, people: 0 };
    ORG_FILTER_NAMESPACES.forEach((ns) => {
        counts[ns] = countVisibleCards(ns);
    });

    const activeTabEl = document.querySelector<HTMLElement>(".organize-tab.active[data-filter-ns]");
    const activeNs = (activeTabEl?.dataset.filterNs as OrgNamespace | undefined) ?? null;

    ORG_FILTER_NAMESPACES.forEach((ns) => {
        const countEl = document.getElementById(`org-tab-count-${ns}`);
        if (!countEl) return;
        if (ns === activeNs) {
            countEl.hidden = true;
        } else {
            countEl.textContent = String(counts[ns]);
            countEl.hidden = false;
        }
    });

    ORG_FILTER_NAMESPACES.forEach((ns) => {
        const footer = document.getElementById(`org-cross-tab-${ns}`);
        if (!footer) return;
        if (ns !== activeNs) {
            footer.hidden = true;
            return;
        }

        const otherParts = ORG_FILTER_NAMESPACES.filter((otherNs) => otherNs !== ns && counts[otherNs] > 0).map((otherNs) => ({
            ns: otherNs,
            n: counts[otherNs],
            label: NS_LABELS[otherNs],
        }));

        if (otherParts.length === 0) {
            footer.hidden = true;
            return;
        }

        const selfCount = counts[ns];
        const prefix = selfCount === 0 ? `No ${NS_LABELS[ns]} match, but ` : "";
        const parts = otherParts.map((p) => {
            const tabKey = p.ns === "cat" ? "categories" : p.ns === "tag" ? "tags" : p.ns;
            const tabBtn = document.querySelector(`.organize-tab[data-tab="${tabKey}"]`);
            return tabBtn ? `<button class="org-cross-tab-link" type="button" data-org-tab="${tabKey}">${p.n} ${p.label}</button>` : `${p.n} ${p.label}`;
        });

        let partsHtml: string;
        if (parts.length === 1) partsHtml = parts[0]!;
        else if (parts.length === 2) partsHtml = `${parts[0]} and ${parts[1]}`;
        else partsHtml = `${parts.slice(0, -1).join(", ")}, and ${parts[parts.length - 1]}`;

        footer.innerHTML = `<i class="material-symbols-outlined">info</i><span>${prefix}${partsHtml} also match this search.</span>`;
        footer.hidden = false;
    });
}

export function syncOrgFilterBarVisibility(activeNs: OrgNamespace | null): void {
    ORG_FILTER_NAMESPACES.forEach((ns) => document.getElementById(`${ns}-filter-bar`)?.classList.remove("open"));
    if (activeNs && (filterBarOpen || hasAnyOrgFilter())) {
        const activeBar = document.getElementById(`${activeNs}-filter-bar`);
        if (activeBar) {
            activeBar.classList.add("open");
            filterBarOpen = true;
        }
    }
    syncFilterBtnActive();
}

export function toggleOrgFilter(ns: OrgNamespace): void {
    const bar = document.getElementById(`${ns}-filter-bar`);
    if (!bar) return;
    const willOpen = !bar.classList.contains("open");
    ORG_FILTER_NAMESPACES.forEach((otherNs) => document.getElementById(`${otherNs}-filter-bar`)?.classList.remove("open"));
    if (willOpen) {
        bar.classList.add("open");
        filterBarOpen = true;
    } else {
        filterBarOpen = false;
        clearOrgFilter(ns);
    }
    syncFilterBtnActive();
}

function toggleOrgChip(btn: HTMLElement, ns: OrgNamespace): void {
    btn.classList.toggle("active");
    const mutex = btn.dataset.mutex;
    if (mutex && btn.classList.contains("active")) {
        document.getElementById(`${ns}-filter-bar`)
            ?.querySelectorAll(`[data-filter="${mutex}"]`)
            .forEach((m) => m.classList.remove("active"));
    }
    if (btn.dataset.filter?.startsWith("color:") && btn.classList.contains("active")) {
        document.getElementById(`${ns}-filter-bar`)
            ?.querySelectorAll(".org-filter-color-dot.active")
            .forEach((d) => {
                if (d !== btn) d.classList.remove("active");
            });
    }
    captureFilterFromBar(ns);
    syncOrgFilterUI();
    applyAllOrgFilters();
}

export function clearOrgFilter(_ns: OrgNamespace): void {
    sharedFilter.search = "";
    sharedFilter.chips = new Set();
    sharedFilter.color = "";
    saveSharedFilter();
    syncOrgFilterUI();
    applyAllOrgFilters();
}

/** Wires the one-time delegated listeners for all filter bars + cross-tab count updates. Call once at page init. */
export function installOrgFilterEngine(): void {
    let crossTabPendingId: ReturnType<typeof setTimeout> | null = null;
    document.addEventListener("org:filter-applied", () => {
        if (crossTabPendingId) clearTimeout(crossTabPendingId);
        crossTabPendingId = setTimeout(updateCrossTabCounts, 0);
    });
    document.addEventListener("org:tab-changed", () => {
        if (hasAnyOrgFilter()) updateCrossTabCounts();
    });

    document.addEventListener("click", (e) => {
        const target = e.target as HTMLElement;
        const crossTabLink = target.closest<HTMLElement>(".org-cross-tab-link[data-org-tab]");
        if (crossTabLink) {
            document.querySelector<HTMLElement>(`.organize-tab[data-tab="${crossTabLink.dataset.orgTab}"]`)?.click();
            return;
        }

        const chip = target.closest<HTMLElement>(".org-filter-chip, .org-filter-color-dot");
        if (chip && !chip.classList.contains("org-filter-clear") && !chip.classList.contains("org-filter-close")) {
            const bar = chip.closest<HTMLElement>(".org-filter-bar");
            const ns = bar?.dataset.filterNs as OrgNamespace | undefined;
            if (ns) toggleOrgChip(chip, ns);
            return;
        }
        const clearBtn = target.closest(".org-filter-clear");
        if (clearBtn) {
            const bar = clearBtn.closest<HTMLElement>(".org-filter-bar");
            const ns = bar?.dataset.filterNs as OrgNamespace | undefined;
            if (ns) clearOrgFilter(ns);
            return;
        }
        const closeBtn = target.closest(".org-filter-close");
        if (closeBtn) {
            const bar = closeBtn.closest<HTMLElement>(".org-filter-bar");
            const ns = bar?.dataset.filterNs as OrgNamespace | undefined;
            if (ns) toggleOrgFilter(ns);
        }
    });

    document.addEventListener("input", (e) => {
        const target = e.target as HTMLElement;
        if (!target.classList.contains("org-filter-search")) return;
        const bar = target.closest<HTMLElement>(".org-filter-bar");
        const ns = bar?.dataset.filterNs as OrgNamespace | undefined;
        if (ns) applyOrgFilter(ns);
    });
}
