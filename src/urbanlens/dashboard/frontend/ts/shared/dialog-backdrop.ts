/**
 * Close a ``<dialog>`` when its backdrop is clicked.
 *
 * A native dialog's backdrop is part of the dialog element itself, so a backdrop
 * click arrives with ``event.target`` set to the dialog and can only be told apart
 * from a click on the dialog's own box by comparing coordinates against its rect.
 *
 * The press is tracked separately from the click so that selecting text inside the
 * dialog and releasing outside it does not close the dialog - a plain click handler
 * would treat that drag as a backdrop click and discard whatever the user was
 * partway through doing.
 *
 * ``data-closefn`` names a global to call instead of ``close()``, for dialogs that
 * need to tear something down (a Leaflet map, a draw session) on the way out.
 *
 * **Testing limitation.** ``_isBackdrop`` compares against ``getBoundingClientRect``,
 * which happy-dom reports as all zeros; the tests alongside this drive the
 * press-then-click sequencing, which is the real logic, and cannot verify the
 * geometry itself.
 *
 * Ported out of ``base.html``'s inline script unchanged.
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
