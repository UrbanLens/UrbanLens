/**
 * Filters the parent-category/tag checkbox list (new-item form and bulk-edit
 * dialog) as the user types. Called via inline oninput= attributes, so it
 * stays a window global - see icon-picker.ts for the same rationale.
 */
export function filterParentOptions(input: HTMLInputElement, containerId: string): void {
    const q = input.value.toLowerCase().trim();
    const container = document.getElementById(containerId);
    if (!container) return;
    container.querySelectorAll<HTMLElement>(".tag-parent-option").forEach((opt) => {
        opt.style.display = !q || (opt.textContent ?? "").toLowerCase().includes(q) ? "" : "none";
    });
}

export function installGlobalParentSearch(): void {
    window.filterParentOptions = filterParentOptions;
}

declare global {
    interface Window {
        filterParentOptions: typeof filterParentOptions;
    }
}
