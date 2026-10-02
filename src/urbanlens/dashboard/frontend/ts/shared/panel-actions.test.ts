import { beforeAll, describe, expect, test } from "bun:test";

import { installPanelActions } from "./panel-actions";

beforeAll(() => {
    installPanelActions();
    installPanelActions();
});

function press(action: string, root: ParentNode = document): void {
    const control = root.querySelector(`[data-panel-action="${action}"]`);
    (control?.querySelector("i") ?? control)?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
}

function el(selector: string): HTMLElement {
    return document.querySelector<HTMLElement>(selector) as HTMLElement;
}

describe("ownership panel", () => {
    function mount(): void {
        document.body.innerHTML = `
          <div class="ownership-panel">
            <button data-panel-action="ownership-add"><i>add</i></button>
            <div class="po-tab-body" data-card-pane="owners">
              <div class="po-owner-row">
                <div class="po-owner-display"><button data-panel-action="owner-edit"><i>edit</i></button></div>
                <form class="po-owner-edit-form" hidden><button type="button" data-panel-action="owner-cancel"><i>close</i></button></form>
              </div>
              <form class="po-add-form" hidden><input class="po-input" id="owner-name"></form>
            </div>
            <div class="po-tab-body" data-card-pane="sales" hidden><p>Loading...</p></div>
          </div>`;
    }

    test("Add opens the visible tab's add form and focuses it, and closes it again", () => {
        mount();
        press("ownership-add");
        expect(el(".po-add-form").hidden).toBe(false);
        expect(document.activeElement?.id).toBe("owner-name");
        press("ownership-add");
        expect(el(".po-add-form").hidden).toBe(true);
    });

    test("Add does nothing on a tab without an add form", () => {
        mount();
        el('[data-card-pane="owners"]').hidden = true;
        el('[data-card-pane="sales"]').hidden = false;
        press("ownership-add");
        expect(el(".po-add-form").hidden).toBe(true);
    });

    test("Edit swaps an owner's display for its form, and Cancel swaps back", () => {
        mount();
        press("owner-edit");
        expect([el(".po-owner-display").hidden, el(".po-owner-edit-form").hidden]).toEqual([true, false]);
        press("owner-cancel");
        expect([el(".po-owner-display").hidden, el(".po-owner-edit-form").hidden]).toEqual([false, true]);
    });
});

describe("aliases and notes", () => {
    test("the aliases Add marks the panel adding and focuses the input", () => {
        document.body.innerHTML = `<div class="aliases-panel"><button data-panel-action="alias-add"><i>add</i></button><input class="alias-add-input" id="alias"></div>`;
        press("alias-add");
        expect(el(".aliases-panel").classList.contains("is-adding")).toBe(true);
        expect(document.activeElement?.id).toBe("alias");
    });

    test("the notes Add focuses the note input", () => {
        document.body.innerHTML = `<div class="pin-notes-panel"><button data-panel-action="note-add"><i>add</i></button><textarea class="pin-note-input" id="note"></textarea></div>`;
        press("note-add");
        expect(document.activeElement?.id).toBe("note");
    });
});

describe("custom fields", () => {
    test("the pin panel's Add toggles its add form, focusing the name", () => {
        document.body.innerHTML = `
          <div class="custom-fields-panel">
            <button data-panel-action="custom-field-add"><i>add</i></button>
            <form class="cf-add-form" hidden><input name="name" id="cf-name"></form>
          </div>`;
        press("custom-field-add");
        expect(el(".cf-add-form").hidden).toBe(false);
        expect(document.activeElement?.id).toBe("cf-name");
        press("custom-field-add");
        expect(el(".cf-add-form").hidden).toBe(true);
    });

    test("a settings row's Edit and Cancel swap its display and form", () => {
        document.body.innerHTML = `
          <li class="cf-field-row">
            <div class="cf-field-display"><button data-panel-action="cf-field-edit"><i>edit</i></button></div>
            <form class="cf-field-edit" hidden><button type="button" data-panel-action="cf-field-cancel"><i>close</i></button></form>
          </li>`;
        press("cf-field-edit");
        expect([el(".cf-field-display").hidden, el("form.cf-field-edit").hidden]).toEqual([true, false]);
        press("cf-field-cancel");
        expect([el(".cf-field-display").hidden, el("form.cf-field-edit").hidden]).toEqual([false, true]);
    });

    test("a settings group's Add field opens its form in place of the trigger, and Cancel resets and closes it", () => {
        document.body.innerHTML = `
          <div class="cf-group">
            <button class="cf-add-trigger" data-panel-action="cf-add-open"><i>add</i></button>
            <form class="cf-add-form" hidden>
              <input class="cf-name-input" id="new-name">
              <button type="button" data-panel-action="cf-add-cancel"><i>close</i></button>
            </form>
          </div>`;
        press("cf-add-open");
        expect([el(".cf-add-trigger").hidden, el(".cf-add-form").hidden]).toEqual([true, false]);
        expect(document.activeElement?.id).toBe("new-name");

        (document.getElementById("new-name") as HTMLInputElement).value = "Gate code";
        press("cf-add-cancel");
        expect([el(".cf-add-trigger").hidden, el(".cf-add-form").hidden]).toEqual([false, true]);
        expect((document.getElementById("new-name") as HTMLInputElement).value).toBe("");
    });

    test("a slider shows its value beside it as it moves", () => {
        document.body.innerHTML = `
          <span class="cf-value-slider"><input type="range" class="cf-slider-input" min="0" max="10" value="2"><output class="cf-slider-output">2</output></span>
          <span class="cf-value-slider"><input type="range" class="cf-slider-input" min="0" max="10" value="5"><output class="cf-slider-output" id="other">5</output></span>`;
        const slider = el(".cf-slider-input") as HTMLInputElement;
        slider.value = "7";
        slider.dispatchEvent(new Event("input", { bubbles: true }));
        expect(el(".cf-slider-output").textContent).toBe("7");
        expect(document.getElementById("other")?.textContent).toBe("5");
    });
});
