/**
 * A picker whose candidates live in a shared <template> fills its popup the first time the popup opens.
 */

import { beforeEach, describe, expect, test } from "bun:test";

import { LabelRelPicker } from "./label-rel-picker";

const CANDIDATES = `
  <template id="label-rel-candidates-priority">
    <button type="button" class="tag-chip label-rel-suggestion" data-id="5" data-kind="tag" data-name="existing parent" onclick="LabelRelPicker.pick(this)">Existing Parent</button>
    <button type="button" class="tag-chip label-rel-suggestion" data-id="6" data-kind="category" data-name="hospital" onclick="LabelRelPicker.pick(this)">Hospital</button>
    <button type="button" class="tag-chip label-rel-suggestion" data-id="9" data-kind="tag" data-name="brand new" onclick="LabelRelPicker.pick(this)">Brand New</button>
  </template>`;

function picker(instanceId: string, mode: string, selectedParent: string): string {
    return `
  <div class="label-rel-picker" data-picker-id="${instanceId}" data-mode="${mode}">
    <div class="label-rel-selected-group">
      <div class="label-rel-add-dropdown">
        <div class="label-rel-popup" id="${instanceId}-popup-parent" hidden>
          <input type="text" class="form-input label-rel-search">
          <div class="label-rel-suggestions" id="${instanceId}-suggestions-parent" data-rel-type="parent" data-candidates-from="priority"></div>
        </div>
      </div>
      <div class="label-rel-selected-chips" id="${instanceId}-sel-parent">${selectedParent}</div>
      <p class="label-rel-empty-hint"${selectedParent ? " hidden" : ""}>No parents selected.</p>
    </div>
    <div class="label-rel-selected-group">
      <div class="label-rel-add-dropdown">
        <div class="label-rel-popup" id="${instanceId}-popup-child" hidden>
          <input type="text" class="form-input label-rel-search">
          <div class="label-rel-suggestions" id="${instanceId}-suggestions-child" data-rel-type="child" data-candidates-from="priority"></div>
        </div>
      </div>
      <div class="label-rel-selected-chips" id="${instanceId}-sel-child"></div>
      <p class="label-rel-empty-hint">No children selected.</p>
    </div>
  </div>`;
}

const SELECTED_FIVE = '<span class="label-rel-chip" data-id="5"><span class="tag-chip">Existing Parent</span></span>';

function suggestions(instanceId: string, relType: string): HTMLElement[] {
    return Array.from(document.querySelectorAll<HTMLElement>(`#${instanceId}-suggestions-${relType} .label-rel-suggestion`));
}

function open(instanceId: string, relType: "parent" | "child"): void {
    const button = document.createElement("button");
    LabelRelPicker.toggle(instanceId, relType, button);
}

beforeEach(() => {
    document.body.innerHTML = CANDIDATES + picker("new-tag", "replace", SELECTED_FIVE) + picker("tag-bulk", "additive", "");
});

describe("LabelRelPicker shared candidates", () => {
    test("a popup renders no candidates until it opens, then every one", () => {
        expect(suggestions("new-tag", "parent")).toHaveLength(0);

        open("new-tag", "parent");

        expect(suggestions("new-tag", "parent").map((button) => button.dataset.id)).toEqual(["5", "6", "9"]);
        expect(suggestions("tag-bulk", "parent")).toHaveLength(0);
    });

    test("a label already chosen arrives hidden, in both directions", () => {
        open("new-tag", "parent");
        open("new-tag", "child");

        for (const relType of ["parent", "child"]) {
            const five = document.querySelector(`#new-tag-suggestions-${relType} [data-id="5"]`);
            expect(five?.classList.contains("label-rel-suggestion--hidden")).toBe(true);
        }
        expect(document.querySelector('#new-tag-suggestions-parent [data-id="6"]')?.classList.contains("label-rel-suggestion--hidden")).toBe(false);
    });

    test("a label created before the popup first opened is offered once", () => {
        document
            .getElementById("new-tag-suggestions-parent")!
            .insertAdjacentHTML("beforeend", '<button type="button" class="tag-chip label-rel-suggestion" data-id="9" data-kind="tag" data-name="brand new">Brand New</button>');

        open("new-tag", "parent");

        expect(document.querySelectorAll('#new-tag-suggestions-parent [data-id="9"]')).toHaveLength(1);
        expect(suggestions("new-tag", "parent")).toHaveLength(3);
    });

    test("opening a popup again does not add its candidates again", () => {
        open("new-tag", "parent");
        open("new-tag", "parent");
        open("new-tag", "parent");

        expect(suggestions("new-tag", "parent")).toHaveLength(3);
    });

    test("a shared candidate picks into the picker whose popup it is in", () => {
        open("tag-bulk", "child");
        const hospital = document.querySelector<HTMLElement>('#tag-bulk-suggestions-child [data-id="6"]')!;

        LabelRelPicker.pick(hospital);

        expect(LabelRelPicker.getSelectedIds("tag-bulk", "child")).toEqual([6]);
        expect(LabelRelPicker.getSelectedIds("new-tag", "child")).toEqual([]);
        expect(hospital.classList.contains("label-rel-suggestion--hidden")).toBe(true);
    });
});
