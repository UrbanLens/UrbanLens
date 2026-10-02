/**
 * Header and row controls of the pin and wiki side panels (ownership, aliases, notes, custom fields), which arrive and
 * re-render by htmx swap. Each control's ``data-panel-action`` names what it does; ``.cf-slider-input`` ranges show
 * their value in the ``.cf-slider-output`` beside them.
 */
import { delegateActions } from "./delegated-actions";

/** Shows or hides *form*, focusing *field* in it once shown. */
function toggleForm(form: HTMLElement | null | undefined, field: string): void {
    if (!form) return;
    form.hidden = !form.hidden;
    if (!form.hidden) form.querySelector<HTMLElement>(field)?.focus();
}

/** Within *control*'s closest *scope*, hides *hide* and shows *show*. */
function swap(control: HTMLElement, scope: string, hide: string, show: string): void {
    const root = control.closest(scope);
    const hidden = root?.querySelector<HTMLElement>(hide);
    const shown = root?.querySelector<HTMLElement>(show);
    if (hidden) hidden.hidden = true;
    if (shown) shown.hidden = false;
}

function onSliderInput(event: Event): void {
    const slider = event.target;
    if (!(slider instanceof HTMLInputElement) || !slider.classList.contains("cf-slider-input")) return;
    const output = slider.parentElement?.querySelector(".cf-slider-output");
    if (output) output.textContent = slider.value;
}

let installed = false;

export function installPanelActions(): void {
    if (installed) return;
    installed = true;
    delegateActions(document, "panel-action", {
        "ownership-add": (control) => {
            const body = control.closest(".ownership-panel")?.querySelector(".po-tab-body:not([hidden])");
            toggleForm(body?.querySelector<HTMLElement>(".po-add-form"), ".po-input");
        },
        "owner-edit": (control) => swap(control, ".po-owner-row", ".po-owner-display", ".po-owner-edit-form"),
        "owner-cancel": (control) => swap(control, ".po-owner-row", ".po-owner-edit-form", ".po-owner-display"),
        "alias-add": (control) => {
            const panel = control.closest(".aliases-panel");
            panel?.classList.add("is-adding");
            panel?.querySelector<HTMLElement>(".alias-add-input")?.focus();
        },
        "note-add": (control) => control.closest(".pin-notes-panel")?.querySelector<HTMLElement>(".pin-note-input")?.focus(),
        "custom-field-add": (control) => toggleForm(control.closest(".custom-fields-panel")?.querySelector<HTMLElement>(".cf-add-form"), "input[name=name]"),
        "cf-field-edit": (control) => swap(control, ".cf-field-row", ".cf-field-display", "form.cf-field-edit"),
        "cf-field-cancel": (control) => swap(control, ".cf-field-row", "form.cf-field-edit", ".cf-field-display"),
        "cf-add-open": (control) => {
            swap(control, ".cf-group", ".cf-add-trigger", ".cf-add-form");
            control.closest(".cf-group")?.querySelector<HTMLElement>(".cf-add-form .cf-name-input")?.focus();
        },
        "cf-add-cancel": (control) => {
            const form = control.closest<HTMLFormElement>("form.cf-add-form");
            if (!form) return;
            form.hidden = true;
            form.reset();
            const trigger = control.closest(".cf-group")?.querySelector<HTMLElement>(".cf-add-trigger");
            if (trigger) trigger.hidden = false;
        },
    });
    document.addEventListener("input", onSliderInput);
}
