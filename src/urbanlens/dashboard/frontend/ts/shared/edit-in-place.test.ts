import { beforeEach, describe, expect, mock, test } from "bun:test";

import { startEditInPlace, type EditInPlaceOptions } from "./edit-in-place";

let el: HTMLElement;

beforeEach(() => {
    document.body.innerHTML = '<span id="t" data-raw-name="Old">Old</span>';
    el = document.getElementById("t") as HTMLElement;
    window.urbanlensSizeEditInPlaceInput = () => undefined;
});

function open(overrides: Partial<EditInPlaceOptions> = {}): { input: HTMLInputElement | HTMLTextAreaElement; save: ReturnType<typeof mock> } {
    const save = mock(() => Promise.resolve());
    startEditInPlace(el, { dataKey: "rawName", inputClass: "x", maxLength: 10, successMessage: "Saved.", save, ...overrides });
    const input = el.querySelector("input, textarea") as HTMLInputElement | HTMLTextAreaElement;
    return { input, save: overrides.save ? (overrides.save as ReturnType<typeof mock>) : save };
}

function key(input: Element, k: string): void {
    input.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true }));
}

const settle = (): Promise<void> => new Promise((r) => setTimeout(r, 0));

describe("startEditInPlace", () => {
    test("Enter saves the trimmed value and shows it", async () => {
        const { input, save } = open();
        input.value = "  New  ";
        key(input, "Enter");
        await settle();
        expect(save).toHaveBeenCalledWith("New");
        expect(el.textContent).toBe("New");
        expect(el.dataset.rawName).toBe("New");
    });

    test("Escape restores the text without saving", () => {
        const { input, save } = open();
        input.value = "New";
        key(input, "Escape");
        expect(save).not.toHaveBeenCalled();
        expect(el.textContent).toBe("Old");
    });

    test("a refused save puts the old text back", async () => {
        const failing = mock(() => Promise.reject(new Error("Name taken.")));
        const { input } = open({ save: failing });
        input.value = "New";
        key(input, "Enter");
        await settle();
        expect(el.textContent).toBe("Old");
        expect(el.dataset.rawName).toBe("Old");
    });

    test("an empty value is refused unless allowed", async () => {
        const { input, save } = open();
        input.value = "   ";
        key(input, "Enter");
        await settle();
        expect(save).not.toHaveBeenCalled();
        expect(el.textContent).toBe("Old");
    });

    test("a multiline editor keeps Enter as a newline and saves on blur, marking an emptied value", async () => {
        const { input, save } = open({ multiline: true, allowEmpty: true, placeholder: "Add one...", emptyClass: "is-empty" });
        expect(input.tagName).toBe("TEXTAREA");
        input.value = "";
        key(input, "Enter");
        expect(save).not.toHaveBeenCalled();
        input.dispatchEvent(new FocusEvent("blur"));
        await settle();
        expect(save).toHaveBeenCalledWith("");
        expect(el.textContent).toBe("Add one...");
        expect(el.classList.contains("is-empty")).toBe(true);
    });

    test("a second click while editing does not open a second editor", () => {
        open();
        startEditInPlace(el, { dataKey: "rawName", inputClass: "x", maxLength: 10, successMessage: "", save: () => Promise.resolve() });
        expect(el.querySelectorAll("input")).toHaveLength(1);
    });
});
