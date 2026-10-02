import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { installPickerActions } from "./picker-actions";

type Call = [string, ...unknown[]];
let calls: Call[] = [];

const realIconPicker = window.IconPicker;
const realPickColor = window.pickColor;

// Stands in for whichever picker the page installed: the map page's and Organize's differ from the shared one.
const recordingPicker = {
    toggle: (id: string) => void calls.push(["toggle", id]),
    setTabSilent: () => {},
    setTab: (id: string, cat: string, btn: HTMLElement) => void calls.push(["setTab", id, cat, btn.id]),
    search: (id: string, query: string) => void calls.push(["search", id, query]),
    pick: (id: string, icon: string, btn: HTMLElement | null) => void calls.push(["pick", id, icon, btn?.id ?? null]),
    _handleUpload: (id: string, input: HTMLInputElement) => void calls.push(["upload", id, input.id]),
};

const ICON_PICKER = `
  <div class="icon-picker-dropdown" data-picker="new-tag">
    <button type="button" id="trigger" data-icon-picker-action="toggle"><span id="current">No icon</span></button>
    <div class="icon-picker-panel">
      <input type="text" class="icon-picker-search-input" id="search">
      <input type="file" id="upload" data-icon-picker-action="upload">
      <button type="button" id="clear" data-icon-picker-action="clear">Clear icon</button>
      <div class="icon-picker-tabs"><button type="button" class="icon-tab" id="all" data-cat="">All</button><button type="button" class="icon-tab" id="things" data-cat="things">Things</button></div>
      <div class="icon-picker-grid">
        <button type="button" class="icon-picker-item icon-picker-none" id="none" data-icon="" data-cat="">None</button>
        <button type="button" class="icon-picker-item" id="camera" data-icon="camera" data-cat="things"><span id="glyph">camera</span></button>
      </div>
    </div>
  </div>`;

const COLOR_PICKER = `
  <div class="color-picker" id="edit-color-picker-7" data-color-value-id="edit-color-value-7">
    <input type="hidden" id="edit-color-value-7">
    <button type="button" class="color-swatch" id="red" data-color="#ff0000"></button>
    <button type="button" class="color-swatch color-clear" id="none-colour" data-color=""><i id="block">block</i></button>
  </div>
  <div class="color-picker" id="unmarked"><button type="button" class="color-swatch" id="loose" data-color="#00ff00"></button></div>`;

function click(id: string): void {
    document.getElementById(id)!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
}

beforeAll(() => {
    installPickerActions();
    installPickerActions();
});

afterAll(() => {
    window.IconPicker = realIconPicker;
    window.pickColor = realPickColor;
});

beforeEach(() => {
    calls = [];
    window.IconPicker = recordingPicker;
    window.pickColor = (pickerId: string, valueId: string, hex: string, btn: HTMLElement) => void calls.push(["pickColor", pickerId, valueId, hex, btn.id]);
    document.body.innerHTML = ICON_PICKER + COLOR_PICKER;
});

describe("the icon picker", () => {
    test("its trigger opens the page's picker, once per click", () => {
        click("current");
        expect(calls).toEqual([["toggle", "new-tag"]]);
    });

    test("a tab filters by its category", () => {
        click("things");
        click("all");
        expect(calls).toEqual([
            ["setTab", "new-tag", "things", "things"],
            ["setTab", "new-tag", "", "all"],
        ]);
    });

    test("an item picks its icon, and None picks no icon", () => {
        click("glyph");
        click("none");
        expect(calls).toEqual([
            ["pick", "new-tag", "camera", "camera"],
            ["pick", "new-tag", "", "none"],
        ]);
    });

    test("Clear icon picks no icon without marking an item", () => {
        click("clear");
        expect(calls).toEqual([["pick", "new-tag", "", null]]);
    });

    test("typing searches", () => {
        const search = document.getElementById("search") as HTMLInputElement;
        search.value = "cam";
        search.dispatchEvent(new Event("input", { bubbles: true }));
        expect(calls).toEqual([["search", "new-tag", "cam"]]);
    });

    test("choosing a file hands it to the picker's upload", () => {
        document.getElementById("upload")!.dispatchEvent(new Event("change", { bubbles: true }));
        expect(calls).toEqual([["upload", "new-tag", "upload"]]);
    });

    test("a page whose picker takes no uploads ignores the file", () => {
        const { _handleUpload: _ignored, ...withoutUpload } = recordingPicker;
        window.IconPicker = withoutUpload;
        document.getElementById("upload")!.dispatchEvent(new Event("change", { bubbles: true }));
        expect(calls).toEqual([]);
    });
});

describe("the colour swatches", () => {
    test("a swatch puts its colour in the field its picker names", () => {
        click("red");
        click("block");
        expect(calls).toEqual([
            ["pickColor", "edit-color-picker-7", "edit-color-value-7", "#ff0000", "red"],
            ["pickColor", "edit-color-picker-7", "edit-color-value-7", "", "none-colour"],
        ]);
    });

    test("a picker that names no field is left to its own page", () => {
        click("loose");
        expect(calls).toEqual([]);
    });
});
