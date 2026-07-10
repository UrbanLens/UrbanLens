import {
  BulkEntityManager,
  installGlobalParentSearch
} from "./categories-tax9frfb.js";
import {
  installGlobalColorPicker,
  installGlobalIconPicker
} from "./categories-26xn7v91.js";

// src/urbanlens/dashboard/frontend/ts/entries/tags.ts
installGlobalIconPicker();
installGlobalColorPicker();
installGlobalParentSearch();
function showNewCustomPreview(input) {
  const file = input.files?.[0];
  if (!file)
    return;
  const preview = document.getElementById("new-custom-preview");
  if (!preview)
    return;
  const reader = new FileReader;
  reader.onload = (e) => {
    preview.src = e.target?.result;
    preview.style.display = "block";
  };
  reader.readAsDataURL(file);
}
window.showNewCustomPreview = showNewCustomPreview;
function resetCustomIconPreview() {
  const preview = document.getElementById("new-custom-preview");
  if (!preview)
    return;
  preview.src = "";
  preview.style.display = "none";
}
function init() {
  const rows = document.getElementById("tag-rows");
  if (!rows)
    return;
  const config = {
    rowsContainerId: "tag-rows",
    cardSelector: ".tag-card[data-tag-id]",
    checkboxSelector: ".tag-select-cb",
    checkboxIdKey: "tagId",
    dataset: {
      id: "tagId",
      name: "tagName",
      color: "tagColor",
      icon: "tagIcon",
      pinCount: "tagPinCount",
      parents: "tagParents",
      customIcon: "tagCustomIcon"
    },
    endpoints: {
      bulkDelete: rows.dataset.bulkDeleteUrl ?? "",
      bulkEdit: rows.dataset.bulkEditUrl ?? "",
      multiMerge: rows.dataset.mergeUrl ?? ""
    },
    labels: {
      entitySingular: "Tag",
      entityPlural: "Tags",
      deleteExtraWarning: "Pins will NOT be deleted.",
      mergeWarning: "All pins will be transferred to the surviving tag. This cannot be undone.",
      emptyIcon: "label"
    },
    selectionBar: {
      barId: "tag-sel-bar",
      countId: "tag-sel-count",
      selectAllId: "tag-sel-all",
      deselectId: "tag-sel-deselect",
      editId: "tag-sel-edit",
      deleteId: "tag-sel-delete",
      mergeId: "tag-sel-merge"
    },
    newForm: {
      formId: "new-tag-form",
      toggleButtonId: "new-tag-btn",
      iconPickerId: "new",
      colorPickerId: "new-color-picker",
      colorValueId: "new-color-value",
      onReset: resetCustomIconPreview
    },
    bulkEditDialog: {
      dialogId: "tag-bulk-edit-dialog",
      titleId: "tag-bulk-edit-title",
      confirmId: "tag-bulk-edit-confirm",
      iconPickerId: "tag-bulk-edit",
      iconWrapId: "tag-bulk-icon-wrap",
      iconNochangeId: "tag-bulk-icon-nochange",
      colorPickerId: "tag-bulk-color-picker",
      colorValueId: "tag-bulk-color-value",
      colorNochangeId: "tag-bulk-color-nochange",
      parentSelectId: "tag-bulk-parent-select",
      parentCheckboxClass: "tag-bulk-parent-cb"
    },
    mergeDialog: {
      dialogId: "tag-merge-dialog",
      titleId: "tag-merge-dialog-title",
      targetCardId: "tag-merge-target-card",
      sourcesListId: "tag-merge-sources-list",
      confirmId: "tag-merge-confirm-btn"
    },
    editDialogBodyId: "tag-edit-dialog-body",
    editDialogId: "tag-edit-dialog",
    editDialogTitleId: "tag-edit-dialog-title",
    mergeFormClass: "tag-merge-form",
    viewStorageKey: "tag_view",
    viewToggleSelector: ".tag-view-btn",
    treeViewConfig: { idKey: "tagId", parentsKey: "tagParents" }
  };
  new BulkEntityManager(config).init();
  document.getElementById("new-custom-input")?.addEventListener("change", (e) => {
    showNewCustomPreview(e.target);
  });
}
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
