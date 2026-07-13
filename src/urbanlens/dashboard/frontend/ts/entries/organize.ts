import { installGlobalOrganizeIconPicker } from "../shared/organize-icon-picker";
import { installGlobalColorPicker } from "../shared/color-picker";
import { installGlobalBadgeRelPicker } from "../shared/badge-rel-picker";
import { installOrgFilterEngine } from "../shared/organize-filter-engine";
import { installOrgBulkToolbar, installOrgTabSwitching, installOrgSectionSwitching, createOrganizeHeader, orgHeader } from "../shared/organize-header";
import { OrgTabManager, type OrgTabManagerConfig } from "../shared/organize-tab-manager";
import { initOrganizePriority } from "../shared/organize-priority";
import { initOnboardingTour } from "../shared/onboarding-tour";

installGlobalOrganizeIconPicker();
installGlobalColorPicker();
installGlobalBadgeRelPicker();

/** Live preview for the "upload custom icon" file inputs on organize's create dialogs. */
function showBadgeCustomPreview(input: HTMLInputElement, previewId: string): void {
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
    showBadgeCustomPreview(input, "new-tag-custom-preview");
}
window.showBadgeCustomPreview = showBadgeCustomPreview;
window.showTagCustomPreview = showTagCustomPreview;
declare global {
    interface Window {
        showBadgeCustomPreview: typeof showBadgeCustomPreview;
        showTagCustomPreview: typeof showTagCustomPreview;
    }
}

/** Destination-tab metadata for badge-kind conversion, shared by the single-item
 *  kindChanged handler and the bulk-convert path below. */
const KIND_ROWS_TARGET: Record<string, string> = { tag: "#tag-rows", category: "#category-rows", status: "#status-rows" };
const KIND_TAB_KEY: Record<string, string> = { tag: "tags", category: "categories", status: "status" };

function buildTabConfig(rows: HTMLElement, overrides: Partial<OrgTabManagerConfig> & Pick<OrgTabManagerConfig, "ns" | "nsCapitalized">): OrgTabManagerConfig {
    const page = document.querySelector<HTMLElement>(".organize-page");
    const rowsUrls: Record<string, string | undefined> = { tag: page?.dataset.rowsUrlTag, category: page?.dataset.rowsUrlCategory, status: page?.dataset.rowsUrlStatus };
    const convertTargets: OrgTabManagerConfig["convertTargets"] = [];
    if (rows.dataset.convertCategoryUrl) convertTargets.push({ kind: "category", label: "Categories", endpoint: rows.dataset.convertCategoryUrl, rowsUrl: rowsUrls.category, rowsTarget: KIND_ROWS_TARGET.category, tabKey: KIND_TAB_KEY.category });
    if (rows.dataset.convertTagUrl) convertTargets.push({ kind: "tag", label: "Tags", endpoint: rows.dataset.convertTagUrl, rowsUrl: rowsUrls.tag, rowsTarget: KIND_ROWS_TARGET.tag, tabKey: KIND_TAB_KEY.tag });
    if (rows.dataset.convertStatusUrl) convertTargets.push({ kind: "status", label: "Statuses", endpoint: rows.dataset.convertStatusUrl, rowsUrl: rowsUrls.status, rowsTarget: KIND_ROWS_TARGET.status, tabKey: KIND_TAB_KEY.status });

    const base: OrgTabManagerConfig = {
        ns: overrides.ns,
        nsCapitalized: overrides.nsCapitalized,
        rowsId: rows.id,
        cardSelector: `.tag-card[data-${overrides.ns}-id]`,
        idKey: `${overrides.ns}Id`,
        nameKey: `${overrides.ns}Name`,
        iconKey: `${overrides.ns}Icon`,
        colorKey: `${overrides.ns}Color`,
        parentsKey: `${overrides.ns}Parents`,
        pinCountKey: `${overrides.ns}PinCount`,
        checkboxSelector: `.${overrides.ns}-select-cb`,
        entitySingular: "",
        entityPluralLower: "",
        entityPluralCap: "",
        emptyIcon: "label",
        endpoints: {
            bulkDelete: rows.dataset.bulkDeleteUrl ?? "",
            bulkEdit: rows.dataset.bulkEditUrl ?? "",
            multiMerge: rows.dataset.mergeUrl ?? "",
            mergeEditTemplate: rows.dataset.mergeEditUrlTemplate,
        },
        supportsMergeEdit: !!rows.dataset.mergeEditUrlTemplate,
        convertTargets,
        newForm: null,
        bulkEditDialog: {
            dialogId: `${overrides.ns}-bulk-edit-dialog`,
            titleId: `${overrides.ns}-bulk-edit-title`,
            confirmId: `${overrides.ns}-bulk-edit-confirm`,
            iconPickerId: `${overrides.ns}-bulk-edit`,
            iconNochangeId: `${overrides.ns}-bulk-icon-nochange`,
            colorPickerId: `${overrides.ns}-bulk-color-picker`,
            colorValueId: `${overrides.ns}-bulk-color-value`,
            colorNochangeId: `${overrides.ns}-bulk-color-nochange`,
            orderValueId: `${overrides.ns}-bulk-order-value`,
            orderNochangeId: `${overrides.ns}-bulk-order-nochange`,
            descValueId: `${overrides.ns}-bulk-description-value`,
            descNochangeId: `${overrides.ns}-bulk-description-nochange`,
            convertHintId: `${overrides.ns}-bulk-convert-hint`,
        },
        mergeDialog: {
            dialogId: `${overrides.ns}-merge-dialog`,
            titleId: `${overrides.ns}-merge-dialog-title`,
            targetCardId: `${overrides.ns}-merge-target-card`,
            sourcesListId: `${overrides.ns}-merge-sources-list`,
            confirmId: `${overrides.ns}-merge-confirm-btn`,
            editNameId: `${overrides.ns}-merge-edit-name`,
            editIconId: `${overrides.ns}-merge-edit`,
            swapHintId: `${overrides.ns}-merge-swap-hint`,
        },
    };
    return { ...base, ...overrides };
}

function initTabs(): void {
    const tagRows = document.getElementById("tag-rows");
    if (tagRows) {
        new OrgTabManager(
            buildTabConfig(tagRows, {
                ns: "tag",
                nsCapitalized: "Tag",
                entitySingular: "Tag",
                entityPluralLower: "tags",
                entityPluralCap: "Tags",
                emptyIcon: "label",
                customIconKey: "tagCustomIcon",
                deleteWarning: "Pins will NOT be deleted.",
                newForm: { dialogId: "new-tag-form", iconPickerId: "new-tag", colorPickerId: "new-tag-color-picker", colorValueId: "new-tag-color-value", customPreviewId: "new-tag-custom-preview" },
            }),
        ).init();
    }

    const catRows = document.getElementById("category-rows");
    if (catRows) {
        new OrgTabManager(
            buildTabConfig(catRows, {
                ns: "cat",
                nsCapitalized: "Cat",
                cardSelector: ".tag-card[data-category-id]",
                idKey: "categoryId",
                nameKey: "categoryName",
                iconKey: "categoryIcon",
                colorKey: "categoryColor",
                parentsKey: "categoryParents",
                pinCountKey: "categoryPinCount",
                locationCountKey: "categoryLocationCount",
                entitySingular: "Category",
                entityPluralLower: "categories",
                entityPluralCap: "Categories",
                emptyIcon: "category",
                deleteWarning: "Pins and locations will NOT be deleted.",
                newForm: { dialogId: "new-category-form", iconPickerId: "new-cat", colorPickerId: "new-cat-color-picker", colorValueId: "new-cat-color-value", customPreviewId: "new-cat-custom-preview" },
            }),
        ).init();
    }

    const statusRows = document.getElementById("status-rows");
    if (statusRows) {
        new OrgTabManager(
            buildTabConfig(statusRows, {
                ns: "status",
                nsCapitalized: "Status",
                entitySingular: "Status",
                entityPluralLower: "statuses",
                entityPluralCap: "Statuses",
                emptyIcon: "flag",
                isProtected: (id) => {
                    const card = document.querySelector<HTMLElement>(`[data-status-id="${id}"]`);
                    return card?.dataset.statusProtected === "true" || card?.dataset.statusProtected === "1";
                },
                newForm: { dialogId: "new-status-form", iconPickerId: "new-status", colorPickerId: "new-status-color-picker", colorValueId: "new-status-color-value", customPreviewId: "new-status-custom-preview" },
            }),
        ).init();
    }

    const peopleRows = document.getElementById("people-badge-rows");
    if (peopleRows) {
        new OrgTabManager(
            buildTabConfig(peopleRows, {
                ns: "people",
                nsCapitalized: "People",
                cardSelector: ".tag-card[data-people-id]",
                idKey: "peopleId",
                nameKey: "peopleName",
                iconKey: "peopleIcon",
                colorKey: "peopleColor",
                parentsKey: "peopleParents",
                pinCountKey: "peoplePinCount",
                checkboxSelector: ".people-sel-cb",
                entitySingular: "Label",
                entityPluralLower: "labels",
                entityPluralCap: "Labels",
                emptyIcon: "person",
                bulkEditDialog: {
                    dialogId: "people-bulk-edit-dialog",
                    titleId: "people-bulk-edit-title",
                    confirmId: "people-bulk-edit-confirm",
                    iconPickerId: "people-bulk-edit",
                    iconNochangeId: "people-bulk-icon-nochange",
                    colorPickerId: "people-bulk-color-picker",
                    colorValueId: "people-bulk-color-value",
                    colorNochangeId: "people-bulk-color-nochange",
                    descValueId: "people-bulk-description-value",
                    descNochangeId: "people-bulk-description-nochange",
                },
                newForm: null,
            }),
        ).init();
    }
}

function initOnboarding(): void {
    const host = document.getElementById("organize-onboarding");
    if (!host) return;
    if (host.dataset.standaloneMode) return;
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
 * Badge edits (icon, color, name, kind, merges, bulk actions) change how pins
 * render on the map without touching any Pin row, so the map's own staleness
 * check (Max(Pin.updated)) can never detect them on its own. Flag the shared
 * cross-page `ul_pins_dirty` marker so the map forces a refresh on its next
 * poll or load, same as the bulk pin importer already does. Same mutations
 * also feed the Display Order tab's priority list, which is otherwise only
 * ever built once at initial page load - tell it to refetch too. (The
 * priority list's own reorder save uses a plain fetch(), not htmx, so this
 * doesn't loop back on itself.)
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

function initConsolidatedDialogOpener(): void {
    document.body.addEventListener("htmx:afterSwap", (e) => {
        const detail = (e as CustomEvent).detail as { target?: HTMLElement };
        const id = detail.target?.id;
        if (!id) return;

        if (id === "badge-edit-dialog-body") {
            const body = detail.target!;
            const titleEl = document.getElementById("badge-edit-dialog-title");
            if (titleEl) {
                if (body.querySelector(".organize-badge-merge-form")) {
                    const mergeName = body.querySelector(".tag-merge-source-name");
                    titleEl.textContent = mergeName ? `Merge ${mergeName.textContent?.trim()}` : "Merge";
                } else if (body.querySelector(".organize-badge-customize-form")) {
                    titleEl.textContent = "Customize Display";
                } else if (body.querySelector(".tag-global-edit-form")) {
                    titleEl.textContent = "Edit Global Tag";
                } else {
                    const kindInput = body.querySelector<HTMLInputElement>('input[name="kind"]:checked');
                    const titles: Record<string, string> = { tag: "Tag", category: "Category", status: "Status" };
                    titleEl.textContent = `Edit ${titles[kindInput?.value ?? ""] ?? "Badge"}`;
                }
            }
            const dialog = document.getElementById("badge-edit-dialog") as HTMLDialogElement | null;
            if (dialog && !dialog.open) dialog.showModal();
        } else if (id === "people-badge-edit-dialog-body") {
            const dialog = document.getElementById("people-badge-edit-dialog") as HTMLDialogElement | null;
            if (dialog && !dialog.open) dialog.showModal();
        }
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
    initConsolidatedDialogOpener();
    initKindChangedListener();
    initPinCacheInvalidation();
    initOnboarding();

    initTabs();
    initOrganizePriority();

    // All tabs must register with the header before it initializes.
    orgHeader.init();
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}
