import { applyAllOrgFilters, clearOrgFilter, syncOrgFilterBarVisibility, syncOrgFilterUI, toggleOrgFilter, type OrgNamespace } from "./organize-filter-engine";

export interface OrgTabConfig {
    filterTitle: string;
    viewAriaLabel: string;
    createTitle: string;
    createHtml: string;
    applyView: () => void;
    onSelAll: () => void;
    updateSelAllBtn: () => void;
    onCreate: () => void;
}

const TAB_FILTER_NS: Record<string, OrgNamespace> = { categories: "cat", tags: "tag", status: "status", people: "people" };

class OrganizeHeader {
    private tabs = new Map<string, OrgTabConfig>();
    private activeTab: string;
    private sharedView: string;
    private actionsEl: HTMLElement | null = null;
    private headerActionsEl: HTMLElement | null = null;
    private filterBtn: HTMLElement | null = null;
    private createBtn: HTMLElement | null = null;
    private viewToggle: HTMLElement | null = null;
    private wired = false;

    constructor(initialTab: string) {
        this.activeTab = initialTab;
        this.sharedView = this.loadSharedView();
    }

    private loadSharedView(): string {
        return (
            localStorage.getItem("organize_view") ??
            localStorage.getItem("tag_view") ??
            localStorage.getItem("category_view") ??
            localStorage.getItem("status_view") ??
            localStorage.getItem("people_view") ??
            "list"
        );
    }

    register(tabKey: string, cfg: OrgTabConfig): void {
        this.tabs.set(tabKey, cfg);
    }

    getFilterNs(): OrgNamespace | null {
        return TAB_FILTER_NS[this.activeTab] ?? null;
    }

    getSharedView(): string {
        return this.sharedView;
    }

    setSharedView(view: string): void {
        this.sharedView = view;
        localStorage.setItem("organize_view", view);
        this.syncViewButtons(view);
        this.tabs.forEach((cfg) => cfg.applyView());
        applyAllOrgFilters();
    }

    syncViewButtons(view: string): void {
        document.querySelectorAll<HTMLElement>(".org-header-view-btn").forEach((btn) => {
            btn.classList.toggle("active", btn.dataset.view === view);
        });
    }

    private syncCreateButton(cfg: OrgTabConfig): void {
        if (!this.createBtn) return;
        this.createBtn.className = "btn btn--primary";
        this.createBtn.style.padding = ".4rem .55rem";
        this.createBtn.style.minWidth = "0";
        this.createBtn.title = cfg.createTitle;
        this.createBtn.innerHTML = cfg.createHtml;
    }

    setTab(tabKey: string): void {
        this.activeTab = tabKey;
        this.headerActionsEl ??= document.querySelector(".organize-page-header-actions");
        this.actionsEl ??= document.getElementById("org-header-actions");
        const cfg = this.tabs.get(tabKey);
        if (this.headerActionsEl) this.headerActionsEl.hidden = tabKey === "priority";
        if (this.actionsEl) this.actionsEl.hidden = !cfg;
        if (!cfg) return;
        if (this.viewToggle) this.viewToggle.setAttribute("aria-label", cfg.viewAriaLabel);
        if (this.filterBtn) this.filterBtn.title = cfg.filterTitle;
        this.syncCreateButton(cfg);
        this.syncViewButtons(this.sharedView);
        cfg.updateSelAllBtn();
    }

    private wireButtons(): void {
        if (this.wired) return;
        this.wired = true;
        this.actionsEl = document.getElementById("org-header-actions");
        this.filterBtn = document.getElementById("org-header-filter-btn");
        const selAllBtn = document.getElementById("org-header-sel-all");
        this.createBtn = document.getElementById("org-header-create-btn");
        this.viewToggle = document.getElementById("org-view-toggle");

        document.querySelectorAll<HTMLElement>(".org-header-view-btn").forEach((btn) => {
            btn.addEventListener("click", () => this.setSharedView(btn.dataset.view ?? "list"));
        });
        selAllBtn?.addEventListener("click", () => this.tabs.get(this.activeTab)?.onSelAll());
        this.filterBtn?.addEventListener("click", () => {
            const ns = this.getFilterNs();
            if (ns) toggleOrgFilter(ns);
        });
        this.createBtn?.addEventListener("click", () => this.tabs.get(this.activeTab)?.onCreate());
    }

    private enforceMobileGalleryFallback(): void {
        if (!window.matchMedia("(max-width: 767px)").matches) return;
        if (this.sharedView === "gallery") this.setSharedView("list");
    }

    init(): void {
        this.wireButtons();
        this.enforceMobileGalleryFallback();
        this.setTab(this.activeTab);
        syncOrgFilterUI();
        applyAllOrgFilters();
        syncOrgFilterBarVisibility(this.getFilterNs());
        window.addEventListener("resize", () => this.enforceMobileGalleryFallback());
    }
}

export let orgHeader: OrganizeHeader;

export function createOrganizeHeader(initialTab: string): OrganizeHeader {
    orgHeader = new OrganizeHeader(initialTab);
    return orgHeader;
}

// ── Shared floating bulk-action toolbar (window globals - called via inline
// onclick in organize/index.html's #org-bulk-bar buttons) ────────────────
export interface OrgBulkHandlers {
    deselect: (() => void) | null;
    edit: (() => void) | null;
    merge: (() => void) | null;
    del: (() => void) | null;
}

function resetOrgBulk(): OrgBulkHandlers {
    return { deselect: null, edit: null, merge: null, del: null };
}

export function installOrgBulkToolbar(): void {
    window._orgBulk = resetOrgBulk();
    window._orgSelectionClearers = window._orgSelectionClearers ?? [];
    window._orgBulkEditByIds = window._orgBulkEditByIds ?? {};

    window._orgRegisterSelectionClearer = (fn) => {
        window._orgSelectionClearers.push(fn);
    };

    window._orgBulkClear = () => {
        document.getElementById("org-bulk-bar")?.classList.remove("visible");
        document.querySelector(".organize-page")?.classList.remove("org-page--has-selection");
        window._orgBulk = resetOrgBulk();
    };

    window._orgClearAllSelections = () => {
        window._orgSelectionClearers.forEach((fn) => fn());
        window._orgBulkClear();
    };

    window._orgBulkSync = (n, opts) => {
        const bar = document.getElementById("org-bulk-bar");
        const countEl = document.getElementById("org-bulk-count");
        const editBtn = document.getElementById("org-bulk-edit-btn") as HTMLButtonElement | null;
        const mergeBtn = document.getElementById("org-bulk-merge-btn") as HTMLButtonElement | null;
        const deleteBtn = document.getElementById("org-bulk-delete-btn") as HTMLButtonElement | null;
        if (!bar) return;

        bar.classList.toggle("visible", n > 0);
        document.querySelector(".organize-page")?.classList.toggle("org-page--has-selection", n > 0);

        if (n > 0 && countEl) countEl.textContent = n === 1 ? "1 selected" : `${n} selected`;
        if (editBtn) editBtn.hidden = !opts.hasEdit;
        if (mergeBtn) {
            mergeBtn.hidden = !opts.hasMerge;
            mergeBtn.disabled = n < 2;
        }
        if (deleteBtn) deleteBtn.hidden = !opts.hasDel;
    };

    // When exactly one badge is selected, "Edit" should open that badge's own
    // single-edit dialog rather than the bulk-edit dialog.
    window._orgOpenSingleEdit = (dataAttr, id) => {
        const card = document.querySelector(`[${dataAttr}="${id}"]`);
        const btn = card?.querySelector<HTMLButtonElement>('.tag-card-actions .btn--icon[title="Edit"]');
        if (!btn) return false;
        btn.click();
        return true;
    };
}

// ── Tab switching --------------------------------------------------------
export function installOrgTabSwitching(): void {
    const tabs = document.querySelectorAll<HTMLElement>(".organize-tab");
    const panels = document.querySelectorAll<HTMLElement>(".organize-panel");

    tabs.forEach((tab) => {
        tab.addEventListener("click", () => {
            const target = tab.dataset.tab;
            if (!target) return;
            tabs.forEach((t) => t.classList.remove("active"));
            tab.classList.add("active");
            panels.forEach((p) => {
                p.hidden = true;
            });
            const panel = document.getElementById(`panel-${target}`);
            if (panel) panel.hidden = false;
            orgHeader.setTab(target);
            localStorage.setItem("organize_tab", target);
            const url = new URL(window.location.href);
            url.searchParams.set("tab", target);
            window.history.replaceState({}, "", url.toString());
            if (target === "priority") window._initPrioritySortable?.();
            document.dispatchEvent(new CustomEvent("org:tab-changed", { detail: { tab: target } }));
            window._orgClearAllSelections?.();
            syncOrgFilterBarVisibility((tab.dataset.filterNs as OrgNamespace | undefined) ?? null);
        });
    });

    document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        const anyOpen = document.querySelector(".org-filter-bar.open");
        if (anyOpen) {
            document.querySelectorAll(".org-filter-bar.open").forEach((bar) => bar.classList.remove("open"));
            const activeTabEl = document.querySelector<HTMLElement>(".organize-tab.active[data-filter-ns]");
            const activeNs = (activeTabEl?.dataset.filterNs as OrgNamespace | undefined) ?? "tag";
            clearOrgFilter(activeNs);
            return;
        }
        window._orgBulk?.deselect?.();
    });
}

// ── Section switching (Badges | Lists | Filters) --------------------------
// A second, independent tab tier above `.organize-tab`/`.organize-panel`:
// switches between the three top-level sections of the Organize page. Lists
// and Filters lazy-load their content via HTMX the first time they're shown
// (hx-trigger="revealed" on `.organize-section-panel`, see organize/index.html) -
// this only ever toggles which section is visible, it never touches that content.
export function installOrgSectionSwitching(): void {
    const tabs = document.querySelectorAll<HTMLElement>(".organize-section-tab");
    const panels = document.querySelectorAll<HTMLElement>(".organize-section-panel");
    if (!tabs.length) return;

    tabs.forEach((tab) => {
        tab.addEventListener("click", () => {
            const section = tab.dataset.section;
            if (!section) return;
            tabs.forEach((t) => t.classList.toggle("is-active", t === tab));
            panels.forEach((p) => {
                p.hidden = p.id !== `panel-${section}`;
            });
            const url = new URL(window.location.href);
            // "badges" isn't a real ?tab= value server-side - it's implied by
            // whichever badge sub-tab (tags/categories/...) was last active,
            // which installOrgTabSwitching persists to localStorage.
            const tabParam = section === "badges" ? (localStorage.getItem("organize_tab") ?? "tags") : section;
            url.searchParams.set("tab", tabParam);
            window.history.replaceState({}, "", url.toString());
        });
    });
}

declare global {
    interface Window {
        _orgBulk: OrgBulkHandlers;
        _orgSelectionClearers: Array<() => void>;
        _orgRegisterSelectionClearer: (fn: () => void) => void;
        _orgClearAllSelections: () => void;
        _orgBulkClear: () => void;
        _orgBulkSync: (n: number, opts: { hasEdit: boolean; hasMerge: boolean; hasDel: boolean }) => void;
        _orgOpenSingleEdit: (dataAttr: string, id: string) => boolean;
        _orgBulkEditByIds: Record<string, (ids: string[]) => void>;
        _initPrioritySortable?: () => void;
    }
}
