/**
 * The Log a Visit / Edit Visit form (``partials/pins/_visit_form.html``), which arrives by htmx on the pin page and
 * the Memories pages: the photo count, and the people not on UrbanLens tagged on the visit.
 */

/** Numbers each added person's fields; unique for the page's life, as the server only needs them distinct. */
let externalRows = 0;

function addExternalRow(button: HTMLElement): void {
    const panel = button.closest(".visit-attachment-panel");
    const template = panel?.querySelector<HTMLTemplateElement>("template.visit-external-row-template");
    const list = panel?.querySelector(".visit-external-list");
    const row = template?.content.firstElementChild?.cloneNode(true);
    if (!list || !(row instanceof HTMLElement)) return;
    externalRows += 1;
    for (const field of row.querySelectorAll<HTMLInputElement>("input[name]")) field.name = field.name.replace("{n}", String(externalRows));
    list.append(row);
    row.querySelector("input")?.focus();
}

function onClick(event: MouseEvent): void {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const add = target.closest<HTMLElement>("[data-external-add]");
    if (add) addExternalRow(add);
    if (target.closest("[data-external-row-remove]")) target.closest(".visit-external-row")?.remove();
    const existing = target.closest("[data-external-remove]")?.closest<HTMLElement>(".visit-external-existing");
    if (existing) {
        const removal = existing.querySelector<HTMLInputElement>('input[name="external_remove"]');
        if (removal) removal.disabled = false;
        existing.hidden = true;
    }
}

function onChange(event: Event): void {
    const input = event.target instanceof HTMLInputElement && event.target.type === "file" ? event.target : null;
    const label = input?.closest(".visit-photo-upload")?.querySelector(".visit-photo-upload-text");
    if (!input || !label) return;
    const count = input.files?.length ?? 0;
    label.textContent = count ? `${count} photo(s) selected` : "Upload new photos";
}

let installed = false;

export function installVisitForm(): void {
    if (installed) return;
    installed = true;
    document.addEventListener("click", onClick);
    document.addEventListener("change", onChange);
}
