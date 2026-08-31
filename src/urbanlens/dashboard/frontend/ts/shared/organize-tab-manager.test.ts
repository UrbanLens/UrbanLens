/**
 * Regression test for the Organize "create label" dialog reuse bug: the
 * dialog is a persistent `<dialog>` (shown/hidden, never removed), so
 * opening it a second time to create another label must not still show the
 * parent/child selections chosen for the *previous* label - only the icon
 * and color pickers were being reset, the relationship picker was not.
 */

import { beforeEach, describe, expect, test } from "bun:test";

import { LabelRelPicker } from "./label-rel-picker";
import { OrgTabManager, type OrgTabManagerConfig } from "./organize-tab-manager";

const CFG: OrgTabManagerConfig = {
    ns: "tag",
    nsCapitalized: "Tag",
    rowsId: "tag-rows",
    cardSelector: ".tag-card",
    idKey: "tagId",
    nameKey: "tagName",
    iconKey: "tagIcon",
    colorKey: "tagColor",
    parentsKey: "tagParents",
    pinCountKey: "tagPinCount",
    checkboxSelector: ".tag-select-cb",
    entitySingular: "Tag",
    entityPluralLower: "tags",
    entityPluralCap: "Tags",
    emptyIcon: "label",
    endpoints: { bulkDelete: "/bulk-delete", bulkEdit: "/bulk-edit", multiMerge: "/merge" },
    supportsMergeEdit: false,
    convertTargets: [],
    newForm: { dialogId: "new-tag-form", iconPickerId: "new-tag", colorPickerId: "new-tag-color-picker", colorValueId: "new-tag-color-value" },
    bulkEditDialog: {
        dialogId: "tag-bulk-edit-dialog",
        titleId: "tag-bulk-edit-title",
        confirmId: "tag-bulk-edit-confirm",
        iconPickerId: "tag-bulk-edit",
        iconNochangeId: "tag-bulk-icon-nochange",
        colorPickerId: "tag-bulk-color-picker",
        colorValueId: "tag-bulk-color-value",
        colorNochangeId: "tag-bulk-color-nochange",
    },
    mergeDialog: { dialogId: "tag-merge-dialog", titleId: "tag-merge-title", targetCardId: "tag-merge-target", sourcesListId: "tag-merge-sources", confirmId: "tag-merge-confirm" },
};

/** Mirrors organize_label_create_dialog.html + _label_relationship_picker.html
 * for instance id "new-tag", with one already-selected parent chip whose
 * matching suggestion is hidden - the state a previous create should have left. */
const DIALOG_MARKUP = `
  <dialog id="new-tag-form">
    <form>
      <div class="label-rel-picker" data-picker-id="new-tag" data-mode="replace">
        <div class="label-rel-selected-chips" id="new-tag-sel-parent">
          <span class="label-rel-chip" data-id="5">
            <span class="tag-chip">Existing Parent<input type="hidden" name="parent_ids" value="5"></span>
            <button type="button" class="tag-chip-remove"></button>
          </span>
        </div>
        <p class="label-rel-empty-hint" hidden>No parents selected.</p>
        <div class="label-rel-selected-chips" id="new-tag-sel-child"></div>
        <p class="label-rel-empty-hint">No children selected.</p>
        <div class="label-rel-popup" id="new-tag-popup-parent" hidden>
          <div class="label-rel-suggestions" id="new-tag-suggestions-parent">
            <button type="button" class="tag-chip label-rel-suggestion label-rel-suggestion--hidden" data-id="5" data-kind="tag" data-name="existing parent"
                    onclick="LabelRelPicker.select('new-tag','parent',this)">Existing Parent</button>
          </div>
        </div>
        <div class="label-rel-popup" id="new-tag-popup-child" hidden>
          <div class="label-rel-suggestions" id="new-tag-suggestions-child"></div>
        </div>
      </div>
    </form>
  </dialog>`;

beforeEach(() => {
    document.body.innerHTML = DIALOG_MARKUP;
});

describe("OrgTabManager onCreate", () => {
    test("clears a parent/child selection left over from the previous label", () => {
        const manager = new OrgTabManager(CFG) as unknown as { onCreate: () => void };

        expect(document.querySelectorAll("#new-tag-sel-parent .label-rel-chip").length).toBe(1);

        manager.onCreate();

        expect(document.querySelectorAll("#new-tag-sel-parent .label-rel-chip").length).toBe(0);
        // The chip's removal should also restore its suggestion as pickable again.
        expect(document.querySelector('#new-tag-suggestions-parent [data-id="5"]')?.classList.contains("label-rel-suggestion--hidden")).toBe(false);
    });

    test("opens the dialog", () => {
        const manager = new OrgTabManager(CFG) as unknown as { onCreate: () => void };
        manager.onCreate();
        expect((document.getElementById("new-tag-form") as HTMLDialogElement).open).toBe(true);
    });

    test("a suggestion added after creating one label is selectable for the next, without a reset clobbering it", () => {
        // Simulates the OOB append LabelCreateView now performs after a
        // successful create: the just-created label becomes a suggestion in
        // the still-open dialog's picker before the user opens it again.
        const container = document.getElementById("new-tag-suggestions-parent")!;
        container.insertAdjacentHTML(
            "beforeend",
            '<button type="button" class="tag-chip label-rel-suggestion" data-id="9" data-kind="tag" data-name="brand new" onclick="LabelRelPicker.select(\'new-tag\',\'parent\',this)">Brand New</button>',
        );

        const manager = new OrgTabManager(CFG) as unknown as { onCreate: () => void };
        manager.onCreate();

        const suggestion = document.querySelector<HTMLElement>('#new-tag-suggestions-parent [data-id="9"]')!;
        expect(suggestion).not.toBeNull();
        LabelRelPicker.select("new-tag", "parent", suggestion);
        expect(LabelRelPicker.getSelectedIds("new-tag", "parent")).toEqual([9]);
    });
});
