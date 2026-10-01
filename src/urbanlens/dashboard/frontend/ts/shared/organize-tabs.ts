import { OrgTabManager, type OrgTabManagerConfig } from "./organize-tab-manager";

/** Destination-tab metadata for label-kind conversion, shared by the single-item
 *  kindChanged handler and the bulk-convert path below. */
export const KIND_ROWS_TARGET: Record<string, string> = { tag: "#tag-rows", category: "#category-rows", status: "#status-rows" };
export const KIND_TAB_KEY: Record<string, string> = { tag: "tags", category: "categories", status: "status" };

type TabOverrides = Partial<OrgTabManagerConfig> & Pick<OrgTabManagerConfig, "ns" | "nsCapitalized">;

function buildTabConfig(rows: HTMLElement, overrides: TabOverrides): OrgTabManagerConfig {
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
        },
        supportsMergeEdit: rows.dataset.mergeEdits === "1",
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

/** People and media labels: kept off the Display Order tab, so no kind conversion and no custom-icon preview. */
function buildNonPriorityTabConfig(rows: HTMLElement, overrides: TabOverrides & Pick<OrgTabManagerConfig, "entitySingular" | "entityPluralLower" | "entityPluralCap" | "emptyIcon">): OrgTabManagerConfig {
    const { ns } = overrides;
    return buildTabConfig(rows, {
        checkboxSelector: `.${ns}-sel-cb`,
        newForm: { dialogId: `new-${ns}-form`, iconPickerId: `new-${ns}`, colorPickerId: `new-${ns}-color-picker`, colorValueId: `new-${ns}-color-value` },
        ...overrides,
    });
}

/** One OrgTabManager config per label tab whose rows container is on the page. */
export function organizeTabConfigs(): OrgTabManagerConfig[] {
    const configs: OrgTabManagerConfig[] = [];
    const tagRows = document.getElementById("tag-rows");
    if (tagRows) {
        configs.push(
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
        );
    }

    const catRows = document.getElementById("category-rows");
    if (catRows) {
        configs.push(
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
        );
    }

    const statusRows = document.getElementById("status-rows");
    if (statusRows) {
        configs.push(
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
        );
    }

    const peopleRows = document.getElementById("people-label-rows");
    if (peopleRows) {
        configs.push(
            buildNonPriorityTabConfig(peopleRows, {
                ns: "people",
                nsCapitalized: "People",
                entitySingular: "Label",
                entityPluralLower: "labels",
                entityPluralCap: "Labels",
                emptyIcon: "person",
            }),
        );
    }

    const mediaRows = document.getElementById("media-label-rows");
    if (mediaRows) {
        configs.push(
            buildNonPriorityTabConfig(mediaRows, {
                ns: "media",
                nsCapitalized: "Media",
                entitySingular: "Media Label",
                entityPluralLower: "media labels",
                entityPluralCap: "Media Labels",
                emptyIcon: "perm_media",
            }),
        );
    }
    return configs;
}

/** Builds and initializes one OrgTabManager per label tab present on the page. */
export function initOrganizeTabs(): void {
    for (const cfg of organizeTabConfigs()) new OrgTabManager(cfg).init();
}

/** Swap targets whose dialog only needs opening; `label-edit-dialog-body` also retitles its dialog. */
const PLAIN_EDIT_DIALOGS: Record<string, string> = {
    "people-label-edit-dialog-body": "people-label-edit-dialog",
    "media-label-edit-dialog-body": "media-label-edit-dialog",
};

/** Opens the edit dialog an htmx edit/merge/customize form was just swapped into. */
export function installOrgEditDialogOpener(): void {
    document.body.addEventListener("htmx:afterSwap", (e) => {
        const detail = (e as CustomEvent).detail as { target?: HTMLElement };
        const id = detail.target?.id;
        if (!id) return;

        let dialogId = PLAIN_EDIT_DIALOGS[id];
        if (id === "label-edit-dialog-body") {
            dialogId = "label-edit-dialog";
            const body = detail.target!;
            const titleEl = document.getElementById("label-edit-dialog-title");
            if (titleEl) {
                if (body.querySelector(".organize-label-merge-form")) {
                    const mergeName = body.querySelector(".tag-merge-source-name");
                    titleEl.textContent = mergeName ? `Merge ${mergeName.textContent?.trim()}` : "Merge";
                } else if (body.querySelector(".organize-label-customize-form")) {
                    titleEl.textContent = "Customize Display";
                } else if (body.querySelector(".tag-global-edit-form")) {
                    titleEl.textContent = "Edit Global Tag";
                } else {
                    const kindInput = body.querySelector<HTMLInputElement>('input[name="kind"]:checked');
                    const titles: Record<string, string> = { tag: "Tag", category: "Category", status: "Status" };
                    titleEl.textContent = `Edit ${titles[kindInput?.value ?? ""] ?? "Label"}`;
                }
            }
        }
        if (!dialogId) return;
        const dialog = document.getElementById(dialogId) as HTMLDialogElement | null;
        if (dialog && !dialog.open) dialog.showModal();
    });
}
