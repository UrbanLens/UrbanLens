/**
 * Opens and closes ``<dialog>`` elements from markup: ``data-dialog-open="<id>"`` shows that dialog modally,
 * ``data-dialog-close`` closes the dialog the control sits in, and ``data-dialog-close="<id>"`` closes the named one.
 *
 * ``data-dialog-open-event="<name>"`` on an opener also fires that event on ``<body>`` once the dialog is open, and
 * each ``data-dialog-fill-<field>="<value>"`` sets the dialog's field of that name first.
 * A dialog whose ``data-closefn`` names a page function is closed through it, as a backdrop click does
 * (``dialog-backdrop.ts``); a closer outside any dialog may name its own.
 */

function pageFunction(name: string | undefined): (() => void) | null {
    const candidate: unknown = name ? Reflect.get(window, name) : undefined;
    return typeof candidate === "function" ? () => candidate() : null;
}

const FILL = "data-dialog-fill-";

function fill(dialog: HTMLDialogElement, opener: HTMLElement): void {
    for (const { name, value } of Array.from(opener.attributes)) {
        if (!name.startsWith(FILL)) continue;
        const field = dialog.querySelector(`[name="${CSS.escape(name.slice(FILL.length))}"]`);
        if (field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement || field instanceof HTMLSelectElement) field.value = value;
    }
}

function onClick(event: MouseEvent): void {
    const target = event.target;
    if (!(target instanceof Element)) return;

    const opener = target.closest<HTMLElement>("[data-dialog-open]");
    if (opener) {
        const dialog = document.getElementById(opener.dataset.dialogOpen ?? "");
        if (!(dialog instanceof HTMLDialogElement)) return;
        fill(dialog, opener);
        if (!dialog.open) dialog.showModal();
        if (opener.dataset.dialogOpenEvent) document.body.dispatchEvent(new Event(opener.dataset.dialogOpenEvent));
        return;
    }

    const closer = target.closest<HTMLElement>("[data-dialog-close]");
    if (!closer) return;
    const named = closer.dataset.dialogClose;
    const dialog = named ? document.getElementById(named) : closer.closest("dialog");
    const custom = pageFunction(closer.dataset.closefn ?? (dialog instanceof HTMLElement ? dialog.dataset.closefn : undefined));
    if (custom) custom();
    else if (dialog instanceof HTMLDialogElement) dialog.close();
}

let installed = false;

export function installGlobalDialogTriggers(): void {
    if (installed) return;
    installed = true;
    document.addEventListener("click", onClick);
}
