/**
 * Typed element lookups that check what they found rather than assert it.
 */

/** The element with ``id`` if it is a ``type``, else null. */
export function byId<T extends HTMLElement>(id: string, type: { new (): T; prototype: T }): T | null {
    const el = document.getElementById(id);
    return el instanceof type ? el : null;
}

export type FormControl = HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;

/** The input, textarea or select with ``id``, for code that only reads or writes ``value``. */
export function formControlById(id: string): FormControl | null {
    const el = document.getElementById(id);
    return el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement || el instanceof HTMLSelectElement ? el : null;
}
