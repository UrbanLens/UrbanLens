/**
 * Actions any template can ask for in markup, installed by the core bundle:
 *
 * - ``data-confirm="<question>"`` on a plain form, or on one of its submit buttons, asks in the site's confirm
 *   dialog before submitting. ``data-confirm-message`` and ``data-confirm-label`` fill in the rest. For an htmx
 *   request, ``hx-confirm`` does this already.
 * - ``data-reload`` on a button reloads the page.
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

function onClick(event: MouseEvent): void {
    const target = event.target instanceof Element ? event.target : null;
    if (target?.closest("[data-reload]")) window.location.reload();
}

let installed = false;

export function installDeclarativeActions(): void {
    if (installed) return;
    installed = true;
    // On document, not body: the core bundle runs in <head>.
    document.addEventListener("submit", (event) => void onSubmit(event));
    document.addEventListener("click", onClick);
}
