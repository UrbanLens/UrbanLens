/**
 * Actions any template can ask for in markup, installed by the core bundle:
 *
 * - ``data-confirm="<question>"`` on a plain form, or on one of its submit buttons, asks in the site's confirm
 *   dialog before submitting. ``data-confirm-message`` and ``data-confirm-label`` fill in the rest. For an htmx
 *   request, ``hx-confirm`` does this already.
 * - ``data-reload`` on a button reloads the page.
 * - ``data-enabled-by="<id> ..."`` keeps a button disabled until every named field is satisfied: a checkbox
 *   ticked, a field with ``data-expect="<phrase>"`` saying that phrase (ignoring case and edge spaces), anything
 *   else filled in. On any other element it disables the fields inside and dims it (``.is-off``) until then.
 * - ``data-reveal="<id>"`` on a button shows that hidden element in its place and focuses its first field; resetting
 *   the form they sit in hides it again.
 * - ``data-navigate`` on a select goes to the address in the chosen option's value.
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
    if (!(field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement)) return false;
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
    if (target instanceof HTMLInputElement && target.type === "checkbox") syncEnabledBy();
    if (target instanceof HTMLSelectElement && target.hasAttribute("data-navigate") && target.value) window.location.assign(target.value);
}

let installed = false;

export function installDeclarativeActions(): void {
    if (installed) return;
    installed = true;
    // On document, not body: the core bundle runs in <head>.
    document.addEventListener("submit", (event) => void onSubmit(event));
    document.addEventListener("click", onClick);
    document.addEventListener("change", onChange);
    document.addEventListener("input", syncEnabledBy);
    document.addEventListener("reset", onReset);
    // close does not bubble.
    document.addEventListener("close", onDialogClose, true);
    document.addEventListener("htmx:load", syncEnabledBy);
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", syncEnabledBy, { once: true });
    else syncEnabledBy();
    // A back/forward visit can restore a ticked box under the server's disabled button.
    window.addEventListener("pageshow", syncEnabledBy);
}
