/**
 * Saves a settings form as it is edited, in place of its Save button: the Settings page's sections and site
 * admin's. The form posts as XHR; a JSON answer may refuse the save (``ok: false`` with ``errors`` or a
 * ``message``) or report ``values`` the server clamped, which are repainted.
 */

import type { FetchInit } from "./site-runtime";

type Control = HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;

export interface FormAutosaveOptions {
    /** The row holding the Save button; the indicator goes there. */
    actionsSelector: string;
    submitSelector: string;
    /** Milliseconds to wait after a ``change`` on this control, or null to ignore it. */
    changeDelay: (control: Control) => number | null;
    /** The same for ``input``. */
    inputDelay: (control: Control) => number | null;
}

const INDICATOR = "form-autosave-indicator";
const NOT_SAVED = "Could not save your changes. Please try again.";

function isControl(target: unknown): target is Control {
    return target instanceof HTMLInputElement || target instanceof HTMLSelectElement || target instanceof HTMLTextAreaElement;
}

/** A password (or an API key) goes only where its own form's submit sends it, derived first where E2EE applies. */
function holdsSecret(form: HTMLFormElement): boolean {
    return form.querySelector("input[type=password]") !== null;
}

/** The server's reason for refusing a save, if it gave one. */
function refusal(data: unknown): string | null {
    if (!data || typeof data !== "object" || Reflect.get(data, "ok") !== false) return null;
    const errors: unknown = Reflect.get(data, "errors");
    if (errors && typeof errors === "object") {
        for (const messages of Object.values(errors)) {
            const [first]: unknown[] = Array.isArray(messages) ? messages : [];
            if (typeof first === "string" && first) return first;
        }
    }
    const message: unknown = Reflect.get(data, "message");
    return typeof message === "string" && message ? message : NOT_SAVED;
}

function clampedValues(data: unknown): [string, string][] {
    const values: unknown = data && typeof data === "object" ? Reflect.get(data, "values") : null;
    if (!values || typeof values !== "object") return [];
    return Object.entries(values).flatMap(([name, value]: [string, unknown]) => (typeof value === "string" || typeof value === "number" ? [[name, String(value)]] : []));
}

export class FormAutosave {
    private readonly generation = new WeakMap<HTMLFormElement, number>();
    private readonly indicators = new WeakMap<HTMLFormElement, HTMLElement>();
    private readonly fades = new WeakMap<HTMLElement, number>();

    constructor(
        private readonly options: FormAutosaveOptions,
        private readonly later: (fn: () => void, ms: number) => void = (fn, ms) => void window.setTimeout(fn, ms),
    ) {}

    attach(form: HTMLFormElement): void {
        if (holdsSecret(form)) return;
        const actions = form.querySelector(this.options.actionsSelector);
        if (actions) {
            let indicator = actions.querySelector<HTMLElement>(`.${INDICATOR}`);
            if (!indicator) {
                indicator = document.createElement("span");
                indicator.className = INDICATOR;
                actions.append(indicator);
            }
            indicator.setAttribute("role", "status");
            this.indicators.set(form, indicator);
            // Not ``hidden``: ``.btn`` sets its own display, which outranks the attribute.
            for (const button of actions.querySelectorAll<HTMLElement>(this.options.submitSelector)) button.style.display = "none";
        }
        form.addEventListener("change", (event) => {
            const delay = isControl(event.target) ? this.options.changeDelay(event.target) : null;
            if (delay !== null) this.schedule(form, delay);
        });
        form.addEventListener("input", (event) => {
            const delay = isControl(event.target) ? this.options.inputDelay(event.target) : null;
            if (delay !== null) this.schedule(form, delay);
        });
    }

    /** Save ``form`` after ``delay`` ms, superseding any save still waiting for it. */
    schedule(form: HTMLFormElement, delay: number): void {
        window.autosaveGuard?.markDirty();
        const ticket = (this.generation.get(form) ?? 0) + 1;
        this.generation.set(form, ticket);
        this.later(() => {
            if (this.generation.get(form) === ticket) void this.save(form);
        }, delay);
    }

    private flash(form: HTMLFormElement, text: string, error: boolean): void {
        const indicator = this.indicators.get(form);
        if (!indicator) return;
        indicator.textContent = text;
        indicator.classList.toggle(`${INDICATOR}--error`, error);
        indicator.classList.add("is-shown");
        const ticket = (this.fades.get(indicator) ?? 0) + 1;
        this.fades.set(indicator, ticket);
        this.later(
            () => {
                if (this.fades.get(indicator) === ticket) indicator.classList.remove("is-shown");
            },
            error ? 4000 : 2500,
        );
    }

    private async save(form: HTMLFormElement): Promise<void> {
        if (holdsSecret(form)) return;
        const guard = window.autosaveGuard;
        guard?.saveStarted();
        try {
            const init: FetchInit = {
                method: "POST",
                body: new FormData(form),
                headers: { "X-CSRFToken": window.csrftoken ?? "", "X-Requested-With": "XMLHttpRequest" },
                __ulReported: true,
            };
            const response = await fetch(form.action || window.location.href, init);
            const isJson = (response.headers.get("content-type") ?? "").includes("application/json");
            const data: unknown = isJson ? await response.json().catch(() => null) : null;
            const reason = refusal(data) ?? (response.ok ? null : NOT_SAVED);
            if (reason) {
                guard?.markDirty();
                this.flash(form, reason === NOT_SAVED ? "Not saved" : reason, true);
                window.toastr?.error(reason);
                return;
            }
            for (const [name, value] of clampedValues(data)) {
                const control = form.elements.namedItem(name);
                if (isControl(control) && document.activeElement !== control) control.value = value;
            }
            guard?.markClean();
            this.flash(form, "✓ Saved", false);
        } catch {
            guard?.markDirty();
            this.flash(form, "Not saved", true);
            window.toastr?.error("Your changes were not saved. Check your connection and try again.");
        } finally {
            guard?.saveFinished();
        }
    }
}
