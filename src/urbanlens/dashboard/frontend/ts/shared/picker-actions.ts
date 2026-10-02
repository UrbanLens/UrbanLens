/**
 * The server-rendered icon and colour pickers ask for their actions in markup. Each page installs its own
 * ``window.IconPicker`` and ``window.pickColor`` (the map page's and Organize's do more than the shared ones), so
 * these call whichever the page has, and do nothing on a page with none.
 *
 * Inside ``.icon-picker-dropdown[data-picker]``: ``[data-icon-picker-action="toggle"]`` opens or closes the picker,
 * ``="clear"`` picks no icon, and ``="upload"`` on a file input takes a custom image; an ``.icon-tab`` filters by its
 * ``data-cat``, an ``.icon-picker-item`` picks its ``data-icon``, and the ``.icon-picker-search-input`` searches.
 *
 * A ``.color-swatch`` inside ``.color-picker[data-color-value-id]`` puts its ``data-color`` in the field named there.
 */

function pickerOf(control: Element): string | undefined {
    return control.closest<HTMLElement>(".icon-picker-dropdown")?.dataset.picker;
}

function onIconClick(target: Element): void {
    const control = target.closest<HTMLElement>("[data-icon-picker-action], .icon-tab, .icon-picker-item");
    const id = control ? pickerOf(control) : undefined;
    const picker = window.IconPicker;
    if (!control || id === undefined || !picker) return;
    const action = control.dataset.iconPickerAction;
    if (action === "toggle") picker.toggle(id);
    else if (action === "clear") picker.pick(id, "", null);
    else if (action) return;
    else if (control.classList.contains("icon-tab")) picker.setTab(id, control.dataset.cat ?? "", control);
    else picker.pick(id, control.dataset.icon ?? "", control);
}

function onSwatchClick(target: Element): void {
    const swatch = target.closest<HTMLElement>(".color-swatch");
    const picker = swatch?.closest<HTMLElement>(".color-picker[data-color-value-id]");
    if (!swatch || !picker) return;
    window.pickColor?.(picker.id, picker.dataset.colorValueId ?? "", swatch.dataset.color ?? "", swatch);
}

function onClick(event: MouseEvent): void {
    if (!(event.target instanceof Element)) return;
    onIconClick(event.target);
    onSwatchClick(event.target);
}

function onInput(event: Event): void {
    const field = event.target;
    if (!(field instanceof HTMLInputElement) || !field.classList.contains("icon-picker-search-input")) return;
    const id = pickerOf(field);
    if (id !== undefined) window.IconPicker?.search(id, field.value);
}

function onChange(event: Event): void {
    const field = event.target;
    if (!(field instanceof HTMLInputElement) || field.dataset.iconPickerAction !== "upload") return;
    const id = pickerOf(field);
    if (id !== undefined) window.IconPicker?._handleUpload?.(id, field);
}

let installed = false;

export function installPickerActions(): void {
    if (installed) return;
    installed = true;
    document.addEventListener("click", onClick);
    document.addEventListener("input", onInput);
    document.addEventListener("change", onChange);
}
