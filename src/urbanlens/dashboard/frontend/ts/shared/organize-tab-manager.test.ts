/**
 * Regression test for the Organize "create label" dialog reuse bug.
 */

import { beforeEach, describe, expect, test } from "bun:test";

import { LabelRelPicker } from "./label-rel-picker";
import { OrgTabManager, type OrgTabManagerConfig } from "./organize-tab-manager";

const CFG: OrgTabManagerConfig = {
    ns: "tag",
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
            <button type="button" class="tag-chip label-rel-suggestion label-rel-suggestion--hidden" data-id="5" data-kind="tag" data-name="existing parent">Existing Parent</button>
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
        // Simulates the OOB append LabelCreateView now performs after a successful create.
        const container = document.getElementById("new-tag-suggestions-parent")!;
        container.insertAdjacentHTML(
            "beforeend",
            '<button type="button" class="tag-chip label-rel-suggestion" data-id="9" data-kind="tag" data-name="brand new">Brand New</button>',
        );

        const manager = new OrgTabManager(CFG) as unknown as { onCreate: () => void };
        manager.onCreate();

        const suggestion = document.querySelector<HTMLElement>('#new-tag-suggestions-parent [data-id="9"]')!;
        expect(suggestion).not.toBeNull();
        LabelRelPicker.select("new-tag", "parent", suggestion);
        expect(LabelRelPicker.getSelectedIds("new-tag", "parent")).toEqual([9]);
    });
});

describe("OrgTabManager merge with edits", () => {
    const realFetch = globalThis.fetch;

    test("sends only the changed fields, with the merge itself, in one request", async () => {
        document.body.innerHTML = `
          <div id="tag-rows">
            <div class="tag-card" data-tag-id="1" data-tag-name="Target" data-tag-icon="home" data-tag-color="#112233"></div>
            <div class="tag-card" data-tag-id="2" data-tag-name="Source" data-tag-icon="" data-tag-color=""></div>
          </div>
          <dialog id="tag-merge-dialog"><div id="tag-merge-sources"></div>
            <input id="tag-merge-edit-name" value="Renamed"><input id="icon-value-tag-merge-edit" value="home"><input id="tag-merge-edit-color" value="#112233">
            <button id="tag-merge-confirm"></button>
          </dialog>`;
        const requests: Array<{ url: string; body: unknown }> = [];
        globalThis.fetch = (async (url: string, init: RequestInit) => {
            requests.push({ url, body: JSON.parse(String(init.body)) });
            return new Response('<div class="tag-empty"></div>', { status: 200, headers: { "Content-Type": "text/html" } });
        }) as unknown as typeof fetch;
        try {
            const cfg = { ...CFG, supportsMergeEdit: true, mergeDialog: { ...CFG.mergeDialog, editNameId: "tag-merge-edit-name" } };
            const manager = new OrgTabManager(cfg) as unknown as { wireMerge: () => void; selected: Set<string>; mergeTargetId: string | null };
            manager.selected = new Set(["1", "2"]);
            manager.mergeTargetId = "1";
            manager.wireMerge();

            document.getElementById("tag-merge-confirm")!.click();
            await new Promise((resolve) => setTimeout(resolve, 0));

            expect(requests).toEqual([{ url: "/merge", body: { target_id: 1, source_ids: [2], name: "Renamed" } }]);
        } finally {
            globalThis.fetch = realFetch;
        }
    });
});

describe("OrgTabManager bulk edit dialog", () => {
    const BULK_MARKUP = `
      <dialog id="tag-bulk-edit-dialog">
        <div class="kind-toggle">
          <button type="button" class="kind-toggle-option" id="tag-bulk-convert-to-category" data-convert-to="category"><span id="to-category-label">Category</span></button>
          <button type="button" class="kind-toggle-option" id="tag-bulk-convert-to-status" data-convert-to="status">Status</button>
        </div>
        <p id="tag-bulk-convert-hint" hidden></p>
        <input type="checkbox" id="tag-bulk-icon-nochange" checked>
        <div class="color-picker" id="tag-bulk-color-picker" data-color-value-id="tag-bulk-color-value">
          <input type="hidden" id="tag-bulk-color-value">
          <button type="button" class="color-swatch" id="red" data-color="#ff0000"></button>
        </div>
        <input type="checkbox" id="tag-bulk-color-nochange" checked>
        <button type="button" id="tag-bulk-edit-confirm">Apply Changes</button>
      </dialog>`;
    const convertTargets: OrgTabManagerConfig["convertTargets"] = [
        { kind: "category", label: "Categories", endpoint: "/to-category" },
        { kind: "status", label: "Statuses", endpoint: "/to-status" },
    ];

    function wired(cfg: OrgTabManagerConfig): void {
        document.body.innerHTML = BULK_MARKUP;
        (new OrgTabManager(cfg) as unknown as { wireBulkEdit: () => void }).wireBulkEdit();
    }

    test("a kind button converts to that kind, and pressing it again stops", () => {
        wired({ ...CFG, convertTargets, bulkEditDialog: { ...CFG.bulkEditDialog, convertHintId: "tag-bulk-convert-hint" } });
        const toCategory = document.getElementById("tag-bulk-convert-to-category")!;
        const confirm = document.getElementById("tag-bulk-edit-confirm")!;

        document.getElementById("to-category-label")!.click();
        expect(toCategory.classList.contains("is-active")).toBe(true);
        expect(confirm.textContent).toContain("Convert to Categories");
        expect(document.getElementById("tag-bulk-convert-hint")!.hidden).toBe(false);

        toCategory.click();
        expect(toCategory.classList.contains("is-active")).toBe(false);
        expect(confirm.textContent).toContain("Apply Changes");
    });

    test("a tab with nothing to convert to ignores the kind buttons", () => {
        wired(CFG);
        document.getElementById("tag-bulk-convert-to-category")!.click();
        expect(document.getElementById("tag-bulk-convert-to-category")!.classList.contains("is-active")).toBe(false);
    });

    test("picking a colour means the colour is changing", () => {
        wired(CFG);
        document.getElementById("red")!.click();
        expect((document.getElementById("tag-bulk-color-nochange") as HTMLInputElement).checked).toBe(false);
    });
});
