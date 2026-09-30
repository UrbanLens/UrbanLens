/**
 * The "Add Labels" dialog of a pin, wiki or photo label panel (``partials/labels/_label_dialog.html``): search and
 * kind-tab filtering, the Labels/Lists top tabs, and reopening the dialog after an add replaces the panel. The photo
 * lightbox's label panel (``partials/labels/_lightbox_media_labels.html``) searches the same way.
 */

const DIALOG = "dialog.tag-add-dialog";

interface Picker {
    root: string;
    create: string;
    createName: string;
    createLabel: string;
}

const DIALOG_PICKER: Picker = { root: DIALOG, create: ".tad-create-row", createName: ".tad-create-name", createLabel: ".tad-create-label" };
const PICKERS: Picker[] = [
    DIALOG_PICKER,
    { root: ".lightbox-labels", create: ".lightbox-labels-create", createName: ".lightbox-labels-create-name", createLabel: ".lightbox-labels-create-label" },
];

function applyFilter(dialog: Element, picker: Picker = DIALOG_PICKER): void {
    const search = dialog.querySelector<HTMLInputElement>(".dialog-search");
    const typed = search?.value.trim() ?? "";
    const q = typed.toLowerCase();
    const kind = dialog.querySelector<HTMLElement>(".tad-tab--active")?.dataset.tab ?? "all";
    let exact = false;
    for (const item of dialog.querySelectorAll<HTMLElement>(".tag-dialog-item")) {
        const name = item.dataset.name ?? "";
        item.hidden = !((!q || name.includes(q)) && (kind === "all" || item.dataset.kind === kind));
        if (q && name === q) exact = true;
    }
    for (const pill of dialog.querySelectorAll<HTMLElement>(".tad-kind-pill")) pill.hidden = kind !== "all";
    const createRow = dialog.querySelector<HTMLElement>(picker.create);
    if (!createRow) return;
    createRow.hidden = !q || exact;
    const nameField = createRow.querySelector<HTMLInputElement>(picker.createName);
    const label = createRow.querySelector<HTMLElement>(picker.createLabel);
    if (nameField) nameField.value = typed;
    if (label) label.textContent = typed;
}

function activate(tab: HTMLElement, selector: string, activeClass: string): void {
    tab.parentElement?.querySelectorAll(selector).forEach((t) => t.classList.toggle(activeClass, t === tab));
}

function onClick(event: MouseEvent): void {
    const target = event.target instanceof Element ? event.target : null;
    const dialog = target?.closest(DIALOG);
    if (!target || !dialog) return;
    const tab = target.closest<HTMLElement>(".tad-tab");
    if (tab) {
        activate(tab, ".tad-tab", "tad-tab--active");
        applyFilter(dialog);
        return;
    }
    const topTab = target.closest<HTMLElement>(".tad-top-tab");
    if (topTab) {
        activate(topTab, ".tad-top-tab", "tad-top-tab--active");
        for (const panel of dialog.querySelectorAll<HTMLElement>(".tad-top-panel")) panel.hidden = panel.dataset.topPanel !== topTab.dataset.topTab;
    }
}

function onInput(event: Event): void {
    const target = event.target;
    if (!(target instanceof HTMLInputElement) || !target.matches(".dialog-search")) return;
    for (const picker of PICKERS) {
        const root = target.closest(picker.root);
        if (!root) continue;
        applyFilter(root, picker);
        return;
    }
}

export function installLabelDialogs(): void {
    document.addEventListener("click", onClick);
    document.addEventListener("input", onInput);

    // An add or remove replaces the whole panel, dialog included; the new one opens where the old one was.
    let reopenId: string | null = null;
    document.addEventListener("htmx:beforeRequest", (event) => {
        const dialog = event.target instanceof Element ? event.target.closest<HTMLDialogElement>(DIALOG) : null;
        if (dialog?.open && dialog.id) reopenId = dialog.id;
    });
    document.addEventListener("htmx:afterRequest", (event) => {
        const successful: unknown = event instanceof CustomEvent && event.detail ? Reflect.get(event.detail, "successful") : true;
        if (successful === false) reopenId = null;
    });
    document.addEventListener("htmx:afterSwap", () => {
        if (!reopenId) return;
        const dialog = document.getElementById(reopenId);
        reopenId = null;
        if (dialog instanceof HTMLDialogElement && !dialog.open) dialog.showModal();
    });
}
