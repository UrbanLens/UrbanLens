import { installGlobalOrganizeIconPicker } from "../shared/organize-icon-picker";
import { installGlobalColorPicker } from "../shared/color-picker";
import { installGlobalLabelRelPicker } from "../shared/label-rel-picker";
import { installOrgFilterEngine } from "../shared/organize-filter-engine";
import { installOrgBulkToolbar, installOrgTabSwitching, installOrgSectionSwitching, installOrgTabPrewarm, createOrganizeHeader, orgHeader } from "../shared/organize-header";
import { initOrganizeTabs, installOrgEditDialogOpener, KIND_ROWS_TARGET, KIND_TAB_KEY } from "../shared/organize-tabs";
import { initOrganizePriority } from "../shared/organize-priority";
import { toast } from "../shared/dialogs";
import { initOnboardingTour } from "../shared/onboarding-tour";
import { installSavedFilterForm } from "../shared/saved-filter-form";

installGlobalOrganizeIconPicker();
installGlobalColorPicker();
installGlobalLabelRelPicker();
// The Filters tab, loaded by htmx, holds the saved-filter dialog.
installSavedFilterForm();

/** Live preview for the "upload custom icon" file inputs on organize's create dialogs. */
function showLabelCustomPreview(input: HTMLInputElement, previewId: string): void {
    const file = input.files?.[0];
    if (!file) return;
    const preview = document.getElementById(previewId) as HTMLImageElement | null;
    if (!preview) return;
    const reader = new FileReader();
    reader.onload = (e) => {
        preview.src = e.target?.result as string;
        preview.style.display = "block";
    };
    reader.readAsDataURL(file);
}
function showTagCustomPreview(input: HTMLInputElement): void {
    showLabelCustomPreview(input, "new-tag-custom-preview");
}
window.showLabelCustomPreview = showLabelCustomPreview;
window.showTagCustomPreview = showTagCustomPreview;
declare global {
    interface Window {
        showLabelCustomPreview: typeof showLabelCustomPreview;
        showTagCustomPreview: typeof showTagCustomPreview;
    }
}

function initOnboarding(): void {
    const host = document.getElementById("organize-onboarding");
    if (!host) return;
    if (!host.dataset.showOnboardingTips) return;

    initOnboardingTour({
        prefix: "ul_onboarding_v1_organize",
        hostSelector: "#organize-onboarding",
        retryEvent: "org:tab-changed",
        cards: [
            {
                id: "priority-order",
                icon: "low_priority",
                target: "#priority-explainer",
                eyebrow: "Map display",
                title: "Display order decides which label wins on the map",
                body: "If a pin has multiple tags, categories, or statuses, the highest item in this list provides the icon/color that appears on the map.",
                button: "Open display order",
                watchSelector: '[data-tab="priority"]',
                action: () => {
                    document.querySelector<HTMLElement>('[data-tab="priority"]')?.click();
                    document.getElementById("priority-explainer")?.scrollIntoView({ behavior: "smooth", block: "center" });
                },
                ready: () => !!document.getElementById("priority-explainer"),
            },
            {
                id: "drag-priority",
                icon: "drag_indicator",
                target: "#priority-list .priority-drag-handle, #priority-list",
                eyebrow: "Reorder visually",
                title: "Drag important labels upward",
                body: "Put more specific labels near the top so the map shows the most meaningful icon when a pin has multiple tags.",
                button: "Go to display order",
                watchSelector: ".priority-drag-handle",
                watchEvent: "pointerdown",
                action: () => {
                    document.querySelector<HTMLElement>('[data-tab="priority"]')?.click();
                    document.getElementById("priority-list")?.scrollIntoView({ behavior: "smooth", block: "center" });
                },
                ready: () => !!document.getElementById("priority-list"),
            },
            {
                id: "bulk-actions",
                icon: "checklist",
                target: "#org-header-sel-all",
                eyebrow: "Cleanup tools",
                title: "Select multiple labels to merge, edit, or delete in batches",
                body: "Bulk selection is useful when consolidating duplicate tags or applying the same icon/color to a group.",
                button: "Try bulk select",
                watchSelector: "#org-header-sel-all",
                action: () => {
                    const btn = document.getElementById("org-header-sel-all");
                    btn?.click();
                    btn?.focus();
                },
                ready: () => !!document.getElementById("org-header-sel-all"),
            },
        ],
    });
}

function initKindChangedListener(): void {
    window.ulHtmxActions?.register("label-saved", (el, event) => {
        const kind = event.detail.xhr?.getResponseHeader("X-Kind-Changed");
        toast.success(kind ? `Converted to ${kind}.` : (el.dataset.savedMessage ?? "Saved."));
    });

    const page = document.querySelector<HTMLElement>(".organize-page");
    const rowUrls: Record<string, string | undefined> = {
        tag: page?.dataset.rowsUrlTag,
        category: page?.dataset.rowsUrlCategory,
        status: page?.dataset.rowsUrlStatus,
    };

    document.body.addEventListener("htmx:afterRequest", (e) => {
        const detail = (e as CustomEvent).detail as { xhr?: XMLHttpRequest; successful?: boolean };
        if (!detail.xhr || !detail.successful) return;
        const kindChanged = detail.xhr.getResponseHeader("X-Kind-Changed");
        if (!kindChanged) return;
        const url = rowUrls[kindChanged];
        const target = KIND_ROWS_TARGET[kindChanged];
        if (url && target) window.htmx?.ajax("GET", url, { target, swap: "innerHTML" });
        const tabKey = KIND_TAB_KEY[kindChanged];
        if (tabKey) document.querySelector<HTMLElement>(`.organize-tab[data-tab="${tabKey}"]`)?.click();
    });
}

/**
 * Label edits (icon, color, name, kind, merges, bulk actions) change how pins render on the map without touching any Pin row, so.
 */
function initPinCacheInvalidation(): void {
    document.body.addEventListener("htmx:afterRequest", (e) => {
        const detail = (e as CustomEvent).detail as { xhr?: XMLHttpRequest; successful?: boolean; requestConfig?: { verb?: string } };
        if (!detail.xhr || !detail.successful) return;
        if (detail.requestConfig?.verb?.toLowerCase() === "get") return;
        try {
            localStorage.setItem("ul_pins_dirty", "1");
        } catch {
            // localStorage unavailable (private browsing, quota) - map falls back to its 2 min poll.
        }
        document.body.dispatchEvent(new Event("refreshPriority"));
    });
}

function init(): void {
    const page = document.querySelector<HTMLElement>(".organize-page");
    if (!page) return;

    installOrgFilterEngine();
    installOrgBulkToolbar();
    createOrganizeHeader(page.dataset.activeTab ?? "tags");
    installOrgTabSwitching();
    installOrgSectionSwitching();
    installOrgTabPrewarm();
    installOrgEditDialogOpener();
    initKindChangedListener();
    initPinCacheInvalidation();
    initOnboarding();

    initOrganizeTabs();
    initOrganizePriority();

    // All tabs must register with the header before it initializes.
    orgHeader.init();
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}
