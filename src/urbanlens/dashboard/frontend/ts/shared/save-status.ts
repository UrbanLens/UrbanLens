/**
 * A save's outcome beside the form that made it: ``<span class="save-status" aria-live="polite" aria-atomic="true">``
 * shows "Saving...", then "Saved" or the failure, and fades a moment later.
 */

export type SaveState = "saving" | "saved" | "error";

const SHOWN_MS = 2500;
const FADE_MS = 500;
const timers = new WeakMap<Element, ReturnType<typeof setTimeout>>();

export function showSaveStatus(el: Element | null | undefined, state: SaveState, text: string): void {
    if (!el) return;
    clearTimeout(timers.get(el));
    el.className = `save-status is-${state}`;
    el.textContent = text;
    if (state === "saving") return;
    timers.set(
        el,
        setTimeout(() => {
            el.classList.add("is-fading");
            timers.set(el, setTimeout(() => (el.className = "save-status"), FADE_MS));
        }, SHOWN_MS),
    );
}

/** The HTTP status on an ``htmx:afterRequest``, or 0 when the request never got one. */
export function requestStatus(event: Event): number {
    const detail: unknown = event instanceof CustomEvent ? event.detail : null;
    const xhr: unknown = detail && typeof detail === "object" ? Reflect.get(detail, "xhr") : null;
    const status: unknown = xhr && typeof xhr === "object" ? Reflect.get(xhr, "status") : null;
    return typeof status === "number" ? status : 0;
}

export const NOT_SAVED = "Not saved";

export const saveFailedText = (status: number): string => `Save failed (${status || "?"})`;
