/**
 * Autosave for the safety check-in form (``pages/safety/detail.html``) and the safety defaults form
 * (``pages/safety/settings.html``). Typing saves after a pause; a slider, a hidden field the map sets, or a contact
 * chip saves at once.
 */

import { fetchJson } from "./fetch-json";
import { getCsrfToken } from "./csrf";
import { initContactPickers, retireContactPickers, setEditToggleState } from "./safety-contact-picker";

const TYPING_DELAY_MS = 800;
const SAVED_HIDE_MS = 1600;

export interface SafetySaveResponse {
    contacts_html?: string;
    title?: string;
    warnings?: string[];
    rejected_contacts?: string[];
}

export interface SafetyAutosaveOptions {
    form: HTMLFormElement;
    status: HTMLElement | null;
    /** Whether the status floats beside the field last edited, hiding itself once saved. */
    floating?: boolean;
    failureMessage: string;
    onSaved?(data: SafetySaveResponse): void;
}

export class SafetyAutosave {
    private saveTimer: number | undefined;
    private hideTimer: number | undefined;
    private lastTarget: Element | null = null;

    constructor(private readonly options: SafetyAutosaveOptions) {}

    install(): void {
        const { form } = this.options;
        form.addEventListener("input", (event) => {
            const target = event.target instanceof Element ? event.target : null;
            if (!target || (target instanceof HTMLInputElement && target.type === "range")) return;
            // The contact search box isn't submitted; its chips save through contactschange.
            if (target.closest('[data-role="contact-picker"]')) return;
            this.lastTarget = target;
            this.schedule(false);
        });
        form.addEventListener("change", (event) => {
            const target = event.target;
            if (!(target instanceof HTMLInputElement)) return;
            this.lastTarget = target;
            if (target.type === "hidden" || target.type === "range") this.schedule(true);
        });
        form.addEventListener("contactschange", (event) => {
            this.lastTarget = event.target instanceof Element ? event.target : null;
            this.schedule(true);
        });
    }

    schedule(immediate: boolean): void {
        window.clearTimeout(this.saveTimer);
        if (immediate) void this.save();
        else this.saveTimer = window.setTimeout(() => void this.save(), TYPING_DELAY_MS);
    }

    private setStatus(text: string, isError: boolean): void {
        const { status, floating } = this.options;
        if (!status) return;
        status.textContent = text;
        status.classList.toggle("safety-autosave-status--error", isError);
        if (!floating) return;
        status.classList.remove("is-hidden");
        this.positionNear(this.lastTarget);
        window.clearTimeout(this.hideTimer);
        if (!isError) this.hideTimer = window.setTimeout(() => status.classList.add("is-hidden"), SAVED_HIDE_MS);
    }

    private positionNear(el: Element | null): void {
        const { status } = this.options;
        if (!el || !status) return;
        const rect = el.getBoundingClientRect();
        status.style.top = `${Math.max(8, rect.top - status.offsetHeight - 8)}px`;
        status.style.left = `${Math.max(8, Math.min(window.innerWidth - status.offsetWidth - 8, rect.left))}px`;
    }

    private async save(): Promise<void> {
        const { form } = this.options;
        this.setStatus("Saving…", false);
        try {
            const data = await fetchJson<SafetySaveResponse>(form.action, {
                method: "POST",
                headers: { "X-CSRFToken": getCsrfToken(), "X-Requested-With": "XMLHttpRequest" },
                body: new FormData(form),
                reportsItsOwnErrors: true,
            });
            this.setStatus("Saved", false);
            this.options.onSaved?.(data ?? {});
        } catch {
            this.setStatus("Could not save", true);
            window.toastr?.error(this.options.failureMessage);
        }
    }
}

function pickerInput(container: HTMLElement): HTMLInputElement | null {
    const el = container.querySelector('[data-role="input"]');
    return el instanceof HTMLInputElement ? el : null;
}

/**
 * Replace the check-in's contact picker with a fresh render, keeping it open if it was being edited. Every save
 * re-renders it, so what is still being typed in its box is carried across.
 */
export function replaceContactPicker(container: HTMLElement, html: string): void {
    const wasEditing = !!container.querySelector(".safety-contact-picker.is-editing");
    const typing = pickerInput(container);
    const pending = typing ? { value: typing.value, focused: document.activeElement === typing, start: typing.selectionStart, end: typing.selectionEnd } : null;
    retireContactPickers(container);
    container.innerHTML = html;
    initContactPickers(container);
    if (wasEditing) {
        container.querySelector(".safety-contact-picker")?.classList.add("is-editing");
        const toggle = container.querySelector<HTMLElement>('[data-role="edit-toggle"]');
        if (toggle) setEditToggleState(toggle, true);
    }
    // After the editing state: a collapsed picker hides its box, and a hidden box can't take focus.
    const fresh = pickerInput(container);
    if (!fresh || !pending) return;
    fresh.value = pending.value;
    if (pending.focused) {
        fresh.focus();
        fresh.setSelectionRange(pending.start, pending.end);
    }
}
