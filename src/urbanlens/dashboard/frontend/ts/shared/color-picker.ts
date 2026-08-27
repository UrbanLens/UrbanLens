/**
 * Color swatch picker shared by categories/tags/organize's create and
 * bulk-edit dialogs. Templates call this via inline onclick attributes
 * (including HTMX-injected edit-form partials), so it stays a window global
 * rather than delegated listeners - see icon-picker.ts for the same rationale.
 */
export function pickColor(pickerId: string, valueId: string, colorHex: string, btn: HTMLElement): void {
    const picker = document.getElementById(pickerId);
    picker?.querySelectorAll(".color-swatch").forEach((b) => b.classList.remove("selected"));
    btn.classList.add("selected");
    setPicked(valueId, colorHex);
}

/**
 * Write the chosen colour into the hidden field and say so.
 *
 * Assigning .value fires nothing, so until now the only way to learn about a
 * pick was to read the field at form-submit time - which is why this picker
 * could only be used inside a form. A panel that has to react to a choice, like
 * the floorplan editor's marker appearance, had no way to hear it.
 *
 * Args:
 *     valueId: The hidden input's id.
 *     colorHex: The chosen colour, or "" for none.
 */
function setPicked(valueId: string, colorHex: string): void {
    const value = document.getElementById(valueId) as HTMLInputElement | null;
    if (!value) return;
    value.value = colorHex;
    value.dispatchEvent(new Event("input", { bubbles: true }));
    value.dispatchEvent(new Event("change", { bubbles: true }));
}

/** Resets a color picker instance back to "no color" (used by new-item form resets). */
export function resetColorPicker(pickerId: string, valueId: string): void {
    document.getElementById(pickerId)
        ?.querySelectorAll(".color-swatch")
        .forEach((b) => b.classList.remove("selected"));
    // Deliberately silent, unlike pickColor: this is a programmatic form reset,
    // not somebody choosing a colour, and anything listening for a pick wants
    // the second thing.
    const value = document.getElementById(valueId) as HTMLInputElement | null;
    if (value) value.value = "";
}

export function installGlobalColorPicker(): void {
    window.pickColor = pickColor;
}

declare global {
    interface Window {
        pickColor: typeof pickColor;
    }
}
