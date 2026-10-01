/**
 * Click-to-edit text: swaps an element's text for an input over the same box, saves on blur or Enter, and
 * restores the text on Escape or a refused save.
 */

import { toast } from "./dialogs";

export interface EditInPlaceOptions {
    /** The ``dataset`` key holding the unformatted value. */
    dataKey: string;
    inputClass: string;
    maxLength: number;
    /** A textarea, where Enter is a newline and only blur saves. */
    multiline?: boolean;
    rows?: number;
    /** Select the current text on open; the default for a single line. */
    selectAll?: boolean;
    /** Grow the textarea with its content. */
    autoGrow?: boolean;
    allowEmpty?: boolean;
    /** Shown in place of an empty saved value. */
    placeholder?: string;
    /** Toggled on the element while its value is empty. */
    emptyClass?: string;
    successMessage: string;
    /** Shown when the save fails; without it the error's own message is. */
    errorMessage?: string;
    save: (value: string) => Promise<unknown>;
    onSaved?: (value: string) => void;
}

function errorText(error: unknown, fallback: string | undefined): string {
    if (fallback) return fallback;
    if (error instanceof Error && error.message) return error.message;
    return typeof error === "string" && error ? error : "Something went wrong.";
}

export function startEditInPlace(el: HTMLElement, opts: EditInPlaceOptions): void {
    if (el.querySelector("input, textarea")) return;
    const rawValue = el.dataset[opts.dataKey] ?? "";
    const input = opts.multiline ? document.createElement("textarea") : document.createElement("input");
    if (input instanceof HTMLInputElement) input.type = "text";
    else input.rows = opts.rows ?? 2;
    input.className = opts.inputClass;
    input.value = rawValue;
    input.maxLength = opts.maxLength;
    if (opts.placeholder && !opts.emptyClass) input.placeholder = opts.placeholder;

    window.urbanlensSizeEditInPlaceInput(el, input);
    const displayText = el.textContent;
    const wasEmpty = opts.emptyClass ? el.classList.contains(opts.emptyClass) : false;
    const restore = (): void => {
        el.textContent = displayText;
        if (opts.emptyClass) el.classList.toggle(opts.emptyClass, wasEmpty);
    };
    el.textContent = "";
    el.appendChild(input);
    input.focus();
    if (opts.selectAll ?? !opts.multiline) input.select();

    const editor: HTMLElement = input;
    if (opts.autoGrow) {
        const grow = (): void => {
            input.style.height = "auto";
            input.style.height = `${input.scrollHeight}px`;
        };
        editor.addEventListener("input", grow);
        grow();
    }

    let done = false;
    const finish = (save: boolean): void => {
        if (done) return;
        done = true;
        const value = input.value.trim();
        if (!save || value === rawValue.trim() || (!opts.allowEmpty && !value)) {
            restore();
            return;
        }
        opts.save(value)
            .then(() => {
                el.dataset[opts.dataKey] = value;
                el.textContent = value || (opts.placeholder ?? "");
                if (opts.emptyClass) el.classList.toggle(opts.emptyClass, !value);
                toast.success(opts.successMessage);
                opts.onSaved?.(value);
            })
            .catch((error: unknown) => {
                restore();
                toast.error(errorText(error, opts.errorMessage));
            });
    };

    editor.addEventListener("blur", () => finish(true));
    editor.addEventListener("keydown", (e) => {
        e.stopPropagation();
        if (!opts.multiline && e.key === "Enter") {
            e.preventDefault();
            input.blur();
        } else if (e.key === "Escape") {
            e.preventDefault();
            finish(false);
        }
    });
}

/**
 * Starts an editor for any element matching ``selector`` on click, or on Enter/Space while it has focus.
 * Delegated on ``document.body``, so it survives the element being swapped out.
 */
export function delegateEditInPlace(selector: string, options: (el: HTMLElement) => EditInPlaceOptions): void {
    document.body.addEventListener("click", (e) => {
        const el = e.target instanceof Element ? e.target.closest<HTMLElement>(selector) : null;
        if (el) startEditInPlace(el, options(el));
    });
    document.body.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        // Only on the element itself: once editing, Enter and Space belong to the input.
        const el = e.target instanceof HTMLElement && e.target.matches(selector) ? e.target : null;
        if (!el) return;
        e.preventDefault();
        startEditInPlace(el, options(el));
    });
}
