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
    const value = document.getElementById(valueId) as HTMLInputElement | null;
    if (value) value.value = colorHex;
}

/** Resets a color picker instance back to "no color" (used by new-item form resets). */
export function resetColorPicker(pickerId: string, valueId: string): void {
    document.getElementById(pickerId)
        ?.querySelectorAll(".color-swatch")
        .forEach((b) => b.classList.remove("selected"));
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
