/**
 * Site admin > Subscriptions (``pages/site_admin_subscriptions.html``): each role's features, pricing, quota and
 * email-limit forms save through htmx on change, and show the save's outcome. The server confirms a save with a
 * ``roleSettingsSaved`` trigger naming the form's field group and role.
 */

import { NOT_SAVED, requestStatus, saveFailedText, showSaveStatus } from "./save-status";

const FORM = ".inline-sub-form";

function formOf(event: Event): HTMLFormElement | null {
    return event.target instanceof Element ? event.target.closest<HTMLFormElement>(FORM) : null;
}

const statusIn = (form: Element | null | undefined): Element | null => form?.querySelector(".save-status") ?? null;

/** Returns the uninstaller. */
export function installSubscriptionsPage(root: Document): () => void {
    const body = root.body;
    const onBefore = (event: Event): void => showSaveStatus(statusIn(formOf(event)), "saving", "Saving...");
    const onAfter = (event: Event): void => {
        const form = formOf(event);
        const status = requestStatus(event);
        if (form && !(status >= 200 && status < 300)) showSaveStatus(statusIn(form), "error", saveFailedText(status));
    };
    const onSaved = (event: Event): void => {
        const detail: unknown = event instanceof CustomEvent ? event.detail : null;
        const group: unknown = detail && typeof detail === "object" ? Reflect.get(detail, "field_group") : null;
        const role: unknown = detail && typeof detail === "object" ? Reflect.get(detail, "role") : null;
        const form = Array.from(root.querySelectorAll<HTMLFormElement>(FORM)).find((f) => f.dataset.fieldGroup === group && f.dataset.role === role);
        showSaveStatus(statusIn(form), "saved", "Saved");
    };
    const onHalted = (event: Event): void => showSaveStatus(statusIn(formOf(event)), "error", NOT_SAVED);
    body.addEventListener("htmx:beforeRequest", onBefore);
    body.addEventListener("htmx:afterRequest", onAfter);
    body.addEventListener("roleSettingsSaved", onSaved);
    body.addEventListener("htmx:validation:halted", onHalted);
    return () => {
        body.removeEventListener("htmx:beforeRequest", onBefore);
        body.removeEventListener("htmx:afterRequest", onAfter);
        body.removeEventListener("roleSettingsSaved", onSaved);
        body.removeEventListener("htmx:validation:halted", onHalted);
    };
}
