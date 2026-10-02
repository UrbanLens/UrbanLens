/**
 * Actions any template can ask for in markup, installed by the core bundle:
 *
 * - ``data-confirm="<question>"`` on a plain form, or on one of its submit buttons, asks in the site's confirm
 *   dialog before submitting. ``data-confirm-message`` and ``data-confirm-label`` fill in the rest. For an htmx
 *   request, ``hx-confirm`` does this already.
 * - ``data-no-submit`` on a form keeps it from submitting itself, Enter included; a script reads its fields instead.
 * - ``data-reload`` on a button reloads the page.
 * - ``data-enabled-by="<id> ..."`` keeps a button disabled until every named field is satisfied: a checkbox
 *   ticked, a field with ``data-expect="<phrase>"`` saying that phrase (ignoring case and edge spaces), a list of
 *   choices with one chosen, anything else filled in. On any other element it disables the fields inside and dims it (``.is-off``) until then.
 * - ``data-reveal="<id>"`` on a button shows that hidden element in its place and focuses its first field; resetting
 *   the form they sit in hides it again.
 * - ``data-navigate`` on a select goes to the address in the chosen option's value.
 * - ``data-picks="<hidden input id>"`` on a group of ``[data-value]`` buttons (swatches, icons) puts the clicked
 *   one's value in that input and marks it ``aria-pressed``.
 * - ``data-readout="<id>"`` on an input shows its value in that element as it changes.
 * - ``data-toggles="<id>"`` on a button shows or hides that panel, marking the button ``.is-open``.
 * - ``data-empties="<id>"`` on a button empties that element.
 * - ``data-autosubmit`` on a file input submits its form once a file is chosen.
 * - ``data-placeholder-ideas="<JSON island id>"`` on a field suggests another of the island's ideas as its placeholder
 *   each time its dialog closes.
 */

import { confirmAction } from "./dialogs";

/** Forms whose next submit has been confirmed already. */
const confirmed = new WeakSet<HTMLFormElement>();

function asker(form: HTMLFormElement, submitter: HTMLElement | null): HTMLElement | null {
    if (submitter?.hasAttribute("data-confirm")) return submitter;
    return form.hasAttribute("data-confirm") ? form : null;
}

async function onSubmit(event: SubmitEvent): Promise<void> {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.hasAttribute("data-no-submit")) {
        event.preventDefault();
        return;
    }
    const submitter = event.submitter instanceof HTMLElement ? event.submitter : null;
    const source = asker(form, submitter);
    if (!source) return;
    if (confirmed.has(form)) {
        confirmed.delete(form);
        return;
    }
    event.preventDefault();
    const ok = await confirmAction({ title: source.dataset.confirm, message: source.dataset.confirmMessage, confirmLabel: source.dataset.confirmLabel, cancelLabel: source.dataset.cancelLabel });
    if (!ok) return;
    confirmed.add(form);
    form.requestSubmit(submitter ?? undefined);
    // A submit that something else cancelled asks again next time.
    confirmed.delete(form);
}

function reveal(button: HTMLElement): void {
    const section = document.getElementById(button.dataset.reveal ?? "");
    if (!section) return;
    section.hidden = false;
    button.hidden = true;
    // Marked so a form reset undoes only what was revealed here, not a section the server rendered open.
    button.dataset.revealed = "";
    section.querySelector<HTMLElement>("input, select, textarea")?.focus();
}

function onReset(event: Event): void {
    const form = event.target instanceof HTMLFormElement ? event.target : null;
    if (!form) return;
    for (const button of form.querySelectorAll<HTMLElement>("[data-reveal][data-revealed]")) {
        const section = document.getElementById(button.dataset.reveal ?? "");
        if (section) section.hidden = true;
        button.hidden = false;
        delete button.dataset.revealed;
    }
}

function onClick(event: MouseEvent): void {
    const target = event.target instanceof Element ? event.target : null;
    if (target?.closest("[data-reload]")) window.location.reload();
    const revealer = target?.closest<HTMLElement>("[data-reveal]");
    if (revealer) reveal(revealer);
    const choice = target?.closest<HTMLElement>("[data-picks] [data-value]");
    if (choice) pick(choice);
    const toggler = target?.closest<HTMLElement>("[data-toggles]");
    const panel = toggler ? document.getElementById(toggler.dataset.toggles ?? "") : null;
    if (toggler && panel) {
        panel.hidden = !panel.hidden;
        toggler.classList.toggle("is-open", !panel.hidden);
        toggler.setAttribute("aria-expanded", String(!panel.hidden));
    }
    const emptier = target?.closest<HTMLElement>("[data-empties]");
    if (emptier) document.getElementById(emptier.dataset.empties ?? "")?.replaceChildren();
}

function pick(choice: HTMLElement): void {
    const group = choice.closest<HTMLElement>("[data-picks]");
    const input = document.getElementById(group?.dataset.picks ?? "");
    if (!group || !(input instanceof HTMLInputElement)) return;
    input.value = choice.dataset.value ?? "";
    for (const other of group.querySelectorAll("[data-value]")) other.setAttribute("aria-pressed", String(other === choice));
}

function onInput(event: Event): void {
    const target = event.target;
    if (target instanceof HTMLInputElement && target.dataset.readout) {
        const readout = document.getElementById(target.dataset.readout);
        if (readout) readout.textContent = target.value;
    }
    syncEnabledBy();
}

function onDialogClose(event: Event): void {
    if (!(event.target instanceof HTMLDialogElement)) return;
    for (const field of event.target.querySelectorAll<HTMLInputElement>("input[data-placeholder-ideas]")) {
        let ideas: unknown;
        try {
            ideas = JSON.parse(document.getElementById(field.dataset.placeholderIdeas ?? "")?.textContent || "[]");
        } catch {
            continue;
        }
        const names = Array.isArray(ideas) ? ideas.filter((idea): idea is string => typeof idea === "string") : [];
        if (names.length) field.placeholder = `e.g. ${names[Math.floor(Math.random() * names.length)]}`;
    }
}

function satisfied(id: string): boolean {
    const field = document.getElementById(id);
    if (!field) return false;
    if (!(field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement)) return field.querySelector("input:checked") !== null;
    if (field instanceof HTMLInputElement && field.type === "checkbox") return field.checked;
    const expected = field.dataset.expect;
    if (expected !== undefined) return field.value.trim().toLowerCase() === expected.trim().toLowerCase();
    return field.value.length > 0;
}

function syncEnabledBy(): void {
    for (const el of document.querySelectorAll<HTMLElement>("[data-enabled-by]")) {
        const ids = (el.dataset.enabledBy ?? "").split(/\s+/).filter(Boolean);
        const on = ids.length > 0 && ids.every(satisfied);
        if (el instanceof HTMLButtonElement) {
            el.disabled = !on;
            continue;
        }
        el.classList.toggle("is-off", !on);
        for (const field of el.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>("input, select, textarea")) field.disabled = !on;
    }
}

function onChange(event: Event): void {
    const target = event.target;
    if (target instanceof HTMLInputElement && (target.type === "checkbox" || target.type === "radio")) syncEnabledBy();
    if (target instanceof HTMLInputElement && target.hasAttribute("data-autosubmit") && target.files?.length) target.form?.requestSubmit();
    if (target instanceof HTMLSelectElement && target.hasAttribute("data-navigate") && target.value && isSameOrigin(target.value)) window.location.assign(target.value);
}

function isSameOrigin(address: string): boolean {
    try {
        return new URL(address, window.location.href).origin === window.location.origin;
    } catch {
        return false;
    }
}

let installed = false;

export function installDeclarativeActions(): void {
    if (installed) return;
    installed = true;
    // On document, not body: the core bundle runs in <head>.
    document.addEventListener("submit", (event) => void onSubmit(event));
    document.addEventListener("click", onClick);
    document.addEventListener("change", onChange);
    document.addEventListener("input", onInput);
    document.addEventListener("reset", onReset);
    // close does not bubble.
    document.addEventListener("close", onDialogClose, true);
    document.addEventListener("htmx:load", syncEnabledBy);
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", syncEnabledBy, { once: true });
    else syncEnabledBy();
    // A back/forward visit can restore a ticked box under the server's disabled button.
    window.addEventListener("pageshow", syncEnabledBy);
}
