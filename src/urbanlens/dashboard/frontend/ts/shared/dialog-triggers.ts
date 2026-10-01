/**
 * Opens and closes ``<dialog>`` elements from markup: ``data-dialog-open="<id>"`` shows that dialog modally,
 * ``data-dialog-close`` closes the dialog the control sits in, and ``data-dialog-close="<id>"`` closes the named one.
 */

function onClick(event: MouseEvent): void {
    const target = event.target;
    if (!(target instanceof Element)) return;

    const opener = target.closest<HTMLElement>("[data-dialog-open]");
    if (opener) {
        const dialog = document.getElementById(opener.dataset.dialogOpen ?? "");
        if (dialog instanceof HTMLDialogElement && !dialog.open) dialog.showModal();
        return;
    }

    const closer = target.closest<HTMLElement>("[data-dialog-close]");
    if (!closer) return;
    const named = closer.dataset.dialogClose;
    const dialog = named ? document.getElementById(named) : closer.closest("dialog");
    if (dialog instanceof HTMLDialogElement) dialog.close();
}

export function installGlobalDialogTriggers(): void {
    document.addEventListener("click", onClick);
}
