/**
 * Site admin > API limits (``pages/site_admin_api_limits.html``): each service's card autosaves through htmx, shows
 * the save's progress, and is found by search or category tab.
 */

import { NOT_SAVED, requestStatus, saveFailedText, showSaveStatus } from "./save-status";

function cardOf(el: Element | null): HTMLElement | null {
    return el?.closest<HTMLElement>(".api-limit-card") ?? null;
}

function formOf(event: Event): HTMLFormElement | null {
    return event.target instanceof Element ? event.target.closest<HTMLFormElement>(".api-limit-form") : null;
}

function showEnabled(card: HTMLElement | null, enabled: boolean): void {
    if (!card) return;
    card.classList.toggle("is-disabled", !enabled);
    const icon = card.querySelector(".api-state-icon");
    if (icon) icon.textContent = enabled ? "cloud" : "cloud_off";
}

/** Returns the uninstaller. */
export function installApiLimitsPage(root: Document): () => void {
    const body = root.body;
    // A card saves every field in one request, so a failure cannot just invert the toggle: it returns to the value the
    // server last confirmed.
    const lastSaved = new WeakMap<HTMLInputElement, boolean>();
    for (const cb of root.querySelectorAll<HTMLInputElement>(".api-enabled-cb")) lastSaved.set(cb, cb.checked);

    const onChange = (event: Event): void => {
        const cb = event.target;
        if (cb instanceof HTMLInputElement && cb.classList.contains("api-enabled-cb")) showEnabled(cardOf(cb), cb.checked);
    };
    const onBefore = (event: Event): void => {
        const card = cardOf(formOf(event));
        if (!card) return;
        card.classList.add("is-saving");
        showSaveStatus(card.querySelector(".save-status"), "saving", "Saving...");
    };
    const onAfter = (event: Event): void => {
        const form = formOf(event);
        const card = cardOf(form);
        if (!form || !card) return;
        card.classList.remove("is-saving");
        const status = requestStatus(event);
        const cb = form.querySelector<HTMLInputElement>(".api-enabled-cb");
        if (status >= 200 && status < 300) {
            if (cb) lastSaved.set(cb, cb.checked);
            return;
        }
        showSaveStatus(card.querySelector(".save-status"), "error", saveFailedText(status));
        if (cb) {
            cb.checked = lastSaved.get(cb) ?? cb.checked;
            showEnabled(card, cb.checked);
        }
    };
    const onHalted = (event: Event): void => showSaveStatus(cardOf(formOf(event))?.querySelector(".save-status"), "error", NOT_SAVED);
    const onSaved = (event: Event): void => {
        const detail: unknown = event instanceof CustomEvent ? event.detail : null;
        const service: unknown = detail && typeof detail === "object" ? Reflect.get(detail, "service") : null;
        if (typeof service !== "string") return;
        const card = Array.from(root.querySelectorAll<HTMLElement>(".api-limit-card")).find((c) => c.dataset.service === service) ?? null;
        showSaveStatus(card?.querySelector(".save-status"), "saved", "Saved");
    };

    const search = root.getElementById("api-limits-search-input");
    let category = "";
    const applyFilters = (): void => {
        const query = search instanceof HTMLInputElement ? search.value.trim().toLowerCase() : "";
        for (const card of root.querySelectorAll<HTMLElement>(".api-limit-card")) {
            card.hidden = !((!category || card.dataset.category === category) && (!query || (card.dataset.searchText ?? "").includes(query)));
        }
    };
    const onTab = (event: Event): void => {
        const tab = event.target instanceof Element ? event.target.closest<HTMLElement>(".api-limits-tab") : null;
        if (!tab) return;
        category = tab.dataset.category ?? "";
        for (const t of root.querySelectorAll(".api-limits-tab")) {
            t.classList.toggle("is-active", t === tab);
            t.setAttribute("aria-selected", t === tab ? "true" : "false");
        }
        applyFilters();
    };

    body.addEventListener("change", onChange);
    body.addEventListener("htmx:beforeRequest", onBefore);
    body.addEventListener("htmx:afterRequest", onAfter);
    body.addEventListener("apiLimitSaved", onSaved);
    body.addEventListener("htmx:validation:halted", onHalted);
    body.addEventListener("click", onTab);
    search?.addEventListener("input", applyFilters);
    return () => {
        body.removeEventListener("change", onChange);
        body.removeEventListener("htmx:beforeRequest", onBefore);
        body.removeEventListener("htmx:afterRequest", onAfter);
        body.removeEventListener("apiLimitSaved", onSaved);
        body.removeEventListener("htmx:validation:halted", onHalted);
        body.removeEventListener("click", onTab);
        search?.removeEventListener("input", applyFilters);
    };
}
