/**
 * Close a ``<dialog>`` when its backdrop is clicked.
 */

type PressOrigin = "backdrop" | "inside" | null;

let press: PressOrigin = null;

function isBackdrop(dialog: HTMLDialogElement, x: number, y: number): boolean {
    const rect = dialog.getBoundingClientRect();
    return x < rect.left || x > rect.right || y < rect.top || y > rect.bottom;
}

function onMouseDown(event: MouseEvent): void {
    const target = event.target as HTMLElement | null;
    const dialog = target?.closest?.("dialog");
    if (!(dialog instanceof HTMLDialogElement) || !dialog.open) {
        press = null;
        return;
    }
    press = isBackdrop(dialog, event.clientX, event.clientY) ? "backdrop" : "inside";
}

function onClick(event: MouseEvent): void {
    const dialog = event.target;
    if (!(dialog instanceof HTMLDialogElement) || !dialog.open) return;
    if (press !== "backdrop") return;
    if (!isBackdrop(dialog, event.clientX, event.clientY)) return;

    const closeFn = dialog.dataset.closefn;
    const custom = closeFn ? (window as unknown as Record<string, unknown>)[closeFn] : null;
    if (typeof custom === "function") custom();
    else dialog.close();
    press = null;
}

/** Reset module state. Test-only. */
export function resetDialogBackdropForTests(): void {
    press = null;
}

export function installGlobalDialogBackdrop(): void {
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("click", onClick);
}
