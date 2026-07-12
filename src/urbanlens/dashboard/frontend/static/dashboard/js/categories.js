import {
  BulkEntityManager,
  installGlobalParentSearch
} from "./categories-gm62b4vj.js";
import {
  installGlobalColorPicker,
  installGlobalIconPicker
} from "./categories-bbs4ykdk.js";
import"./categories-cm6bs6jx.js";

// src/urbanlens/dashboard/frontend/ts/entries/categories.ts
installGlobalIconPicker();
installGlobalColorPicker();
installGlobalParentSearch();
function init() {
  const rows = document.getElementById("category-rows");
  if (!rows)
    return;
  const config = {
    rowsContainerId: "category-rows",
    cardSelector: ".tag-card[data-category-id]",
    checkboxSelector: ".cat-select-cb",
    checkboxIdKey: "catId",
    dataset: {
      id: "categoryId",
      name: "categoryName",
      color: "categoryColor",
      icon: "categoryIcon",
      pinCount: "categoryPinCount",
      parents: "categoryParents",
      locationCount: "categoryLocationCount"
    },
    endpoints: {
      bulkDelete: rows.dataset.bulkDeleteUrl ?? "",
      bulkEdit: rows.dataset.bulkEditUrl ?? "",
      multiMerge: rows.dataset.mergeUrl ?? ""
    },
    labels: {
      entitySingular: "Category",
      entityPlural: "Categories",
      deleteExtraWarning: "Pins and locations will NOT be deleted.",
      mergeWarning: "All pins and locations will be transferred to the surviving category. This cannot be undone.",
      emptyIcon: "category"
    },
    selectionBar: {
      barId: "cat-sel-bar",
      countId: "cat-sel-count",
      selectAllId: "cat-sel-all",
      deselectId: "cat-sel-deselect",
      editId: "cat-sel-edit",
      deleteId: "cat-sel-delete",
      mergeId: "cat-sel-merge"
    },
    newForm: {
      formId: "new-category-form",
      toggleButtonId: "new-category-btn",
      iconPickerId: "new-cat",
      colorPickerId: "new-cat-color-picker",
      colorValueId: "new-cat-color-value"
    },
    bulkEditDialog: {
      dialogId: "cat-bulk-edit-dialog",
      titleId: "cat-bulk-edit-title",
      confirmId: "cat-bulk-edit-confirm",
      iconPickerId: "bulk-edit",
      iconWrapId: "cat-bulk-icon-wrap",
      iconNochangeId: "cat-bulk-icon-nochange",
      colorPickerId: "cat-bulk-color-picker",
      colorValueId: "cat-bulk-color-value",
      colorNochangeId: "cat-bulk-color-nochange",
      parentSelectId: "cat-bulk-parent-select",
      parentCheckboxClass: "cat-bulk-parent-cb"
    },
    mergeDialog: {
      dialogId: "cat-merge-dialog",
      titleId: "cat-merge-dialog-title",
      targetCardId: "cat-merge-target-card",
      sourcesListId: "cat-merge-sources-list",
      confirmId: "cat-merge-confirm-btn"
    },
    editDialogBodyId: "category-edit-dialog-body",
    editDialogId: "category-edit-dialog",
    editDialogTitleId: "category-edit-dialog-title",
    mergeFormClass: "category-merge-form",
    viewStorageKey: "category_view",
    viewToggleSelector: ".tag-view-btn",
    treeViewConfig: { idKey: "categoryId", parentsKey: "categoryParents" }
  };
  new BulkEntityManager(config).init();
}
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
