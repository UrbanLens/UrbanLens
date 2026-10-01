/**
 * The "add to a list" picker (_add_to_list_dialog.html on the map, the Lists tab of a pin's Organize dialog):
 * a searchable list of the viewer's lists plus a row that creates a new one. The page decides what "add"
 * means; this handles the controls.
 */

import { byId } from "./dom";

export interface AddToListHandlers {
    /** Adds to the list named by slug or uuid. ``control`` is the button pressed. */
    add: (listRef: string, control: HTMLElement) => void;
    /** Creates a list with the typed name, then adds to it. */
    create: (name: string, nameInput: HTMLInputElement, control: HTMLElement) => void;
}

function resetCreateRow(): void {
    const toggleRow = document.getElementById("add-to-list-new-toggle-row");
    const formRow = document.getElementById("add-to-list-new-form-row");
    const nameInput = byId("add-to-list-new-name", HTMLInputElement);
    if (toggleRow) toggleRow.hidden = false;
    if (formRow) formRow.hidden = true;
    if (nameInput) nameInput.value = "";
}

export function filterListPicker(query: string): void {
    const q = query.trim().toLowerCase();
    document.querySelectorAll<HTMLElement>("#add-to-list-results li").forEach((li) => {
        li.style.display = !q || (li.dataset.search ?? "").includes(q) ? "" : "none";
    });
}

export function installAddToListPicker(handlers: AddToListHandlers): void {
    document.addEventListener("click", (e) => {
        const control = e.target instanceof Element ? e.target.closest<HTMLElement>("[data-list-action]") : null;
        if (!control) return;
        switch (control.dataset.listAction) {
            case "show-create": {
                const formRow = document.getElementById("add-to-list-new-form-row");
                const toggleRow = document.getElementById("add-to-list-new-toggle-row");
                if (formRow) formRow.hidden = false;
                if (toggleRow) toggleRow.hidden = true;
                document.getElementById("add-to-list-new-name")?.focus();
                break;
            }
            case "add":
                handlers.add(control.dataset.listRef ?? "", control);
                break;
            case "create": {
                const nameInput = byId("add-to-list-new-name", HTMLInputElement);
                if (!nameInput) return;
                const name = nameInput.value.trim();
                if (name) handlers.create(name, nameInput, control);
                else nameInput.focus();
                break;
            }
        }
    });
    document.addEventListener("input", (e) => {
        if (e.target instanceof HTMLInputElement && e.target.matches("[data-list-filter]")) filterListPicker(e.target.value);
    });
    // ``close`` does not bubble.
    document.addEventListener(
        "close",
        (e) => {
            if (e.target instanceof HTMLDialogElement && e.target.querySelector("#add-to-list-results")) resetCreateRow();
        },
        true,
    );
}
