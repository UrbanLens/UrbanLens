/**
 * The profile page's editable privacy hints (``partials/ui/_privacy_hint.html``): a lock that turns into a picker
 * and saves the one setting it names.
 */

import type { FetchInit } from "./site-runtime";

const FAILED = "Could not update privacy setting.";

function isHintSelect(target: unknown): target is HTMLSelectElement {
    return target instanceof HTMLSelectElement && target.matches(".ul-privacy-hint-select");
}

/** The value the server holds, which is what a refused choice goes back to. */
function savedValue(select: HTMLSelectElement): string {
    return select.dataset.saved ?? Array.from(select.options).find((option) => option.hasAttribute("selected"))?.value ?? select.value;
}

function close(select: HTMLSelectElement): void {
    select.hidden = true;
    const button = select.previousElementSibling;
    if (button instanceof HTMLElement) button.hidden = false;
}

function show(field: string, value: string, display: string): void {
    for (const select of document.querySelectorAll<HTMLSelectElement>(".ul-privacy-hint-select")) {
        if (select.dataset.field !== field) continue;
        select.value = value;
        select.dataset.saved = value;
        const button = select.previousElementSibling;
        if (!(button instanceof HTMLElement)) continue;
        const hint = select.closest<HTMLElement>(".ul-privacy-hint")?.dataset;
        button.title = `${hint?.hintLabel ? `${hint.hintLabel} - ` : ""}Visible to: ${display}${hint?.hintNote ? ` - ${hint.hintNote}` : ""} - click to change`;
        button.setAttribute("aria-label", `Change who can see this - currently visible to: ${display}`);
        const icon = button.querySelector("i");
        if (icon) icon.textContent = value === "anyone" ? "visibility" : "lock";
    }
}

async function save(select: HTMLSelectElement): Promise<void> {
    const field = select.dataset.field ?? "";
    const body = new FormData();
    body.set("value", select.value);
    const init: FetchInit = { method: "POST", body, headers: { "X-CSRFToken": window.csrftoken ?? "", "X-Requested-With": "XMLHttpRequest" }, __ulReported: true };
    try {
        const response = await fetch(select.dataset.url ?? "", init);
        const data: unknown = await response.json().catch(() => null);
        const field_ = (name: string): unknown => (data && typeof data === "object" ? Reflect.get(data, name) : undefined);
        const value = field_("value");
        const display = field_("display");
        if (response.ok && field_("ok") === true && typeof value === "string" && typeof display === "string") {
            show(field, value, display);
            window.toastr?.success("Privacy setting updated.");
            return;
        }
        const reason = field_("error");
        select.value = savedValue(select);
        window.toastr?.error(typeof reason === "string" && reason ? reason : FAILED);
    } catch {
        select.value = savedValue(select);
        window.toastr?.error(FAILED);
    } finally {
        close(select);
    }
}

let installed = false;

export function installPrivacyHints(): void {
    if (installed) return;
    installed = true;
    document.addEventListener("click", (event) => {
        const button = event.target instanceof Element ? event.target.closest<HTMLElement>(".ul-privacy-hint-btn") : null;
        const select = button?.nextElementSibling;
        if (!button || !isHintSelect(select)) return;
        button.hidden = true;
        select.hidden = false;
        select.focus();
        try {
            select.showPicker?.();
        } catch {
            // Not allowed here; a click on the select still opens it.
        }
    });
    document.addEventListener("change", (event) => {
        if (isHintSelect(event.target)) void save(event.target);
    });
    document.addEventListener("focusout", (event) => {
        if (isHintSelect(event.target)) close(event.target);
    });
}
