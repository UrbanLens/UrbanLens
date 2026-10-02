/**
 * What every base.html page needs before its own scripts run: the CSRF token, toast settings, the Django
 * messages and HX-Trigger toasts, the generic request-failure toasts (including the net under raw fetch()),
 * the double-submit lock, and the profile-preview click guard.
 *
 * core.js runs in <head>, before <body> exists, so listeners go on document.
 */

import { raiseToasts, toast, toastWithAction } from "./dialogs";

export const TOAST_TIMEOUT_MS = 4500;

type ToastLevel = "success" | "info" | "warning" | "error";

function toastLevel(value: unknown): ToastLevel {
    return value === "success" || value === "warning" || value === "error" ? value : "info";
}

function toastError(message: string): void {
    toast.error(message || "Request failed. Please try again.");
}

/** Reads ``<meta name="csrf-token">`` into ``window.csrftoken`` (inline scripts read it as a bare global) and sends it with every htmx request. */
export function installCsrfToken(): void {
    const token = document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content;
    if (token) window.csrftoken = token;
    document.addEventListener("htmx:configRequest", (event) => {
        const detail = (event as CustomEvent<{ headers: Record<string, string> }>).detail;
        if (window.csrftoken) detail.headers["X-CSRFToken"] = window.csrftoken;
    });
}

export function configureToastr(): void {
    if (!window.toastr) return;
    window.toastr.options = {
        positionClass: "toast-bottom-right",
        progressBar: true,
        timeOut: TOAST_TIMEOUT_MS,
        extendedTimeOut: 2000,
        closeButton: false,
        tapToDismiss: true,
        onHoverTimeOut: true,
        newestOnTop: true,
        showDuration: 280,
        hideDuration: 220,
        // toastr renders with .html() by default, and server error strings can echo the caller's own input.
        escapeHtml: true,
    };
    // Wrapped here rather than in dialogs.ts's toast, so a direct window.toastr call is lifted above a dialog too.
    const library = window.toastr;
    for (const kind of ["success", "error", "warning", "info"] as const) {
        const show = library[kind].bind(library);
        library[kind] = (message, title, options) => {
            const shown = show(message, title, options);
            raiseToasts();
            return shown;
        };
    }
}

/** Django's messages arrive as ``<template id="ul-messages">`` children carrying ``data-level``. */
export function showServerMessages(): void {
    const holder = document.getElementById("ul-messages");
    if (!(holder instanceof HTMLTemplateElement)) return;
    holder.content.querySelectorAll<HTMLElement>("[data-level]").forEach((message) => {
        toast[toastLevel(message.dataset.level)](message.textContent ?? "");
    });
}

/** Short plain-text bodies (validation messages) are shown as sent; anything else gets the status. */
export function responseErrorMessage(status: number | undefined, responseText: string | undefined): string {
    const text = (responseText ?? "").trim();
    if (text && text.length <= 300 && !text.includes("<")) return text;
    return `Request failed${status ? ` (HTTP ${status})` : ""}.`;
}

/** What the server's ScriptLoginRefusalMiddleware says when a script's request needs a session that has ended. */
export const SESSION_ENDED_MESSAGE = "Your session has ended. Sign in again to continue.";

export type FetchInit = RequestInit & { __ulReported?: boolean };
type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
type WrappedFetch = typeof fetch & { __urbanLensWrapped?: boolean };

const isTimeout = (reason: unknown): boolean => reason instanceof Error && (reason.name === "TimeoutError" || reason.name === "AbortError");

/**
 * A net under every raw fetch(): a non-2xx or a network failure toasts, unless the caller marks the request
 * ``__ulReported`` because it reports its own errors (shared/fetch-json.ts does). A request the caller aborted
 * itself is not a failure, unless it aborted with a ``TimeoutError`` reason.
 */
export function wrapFetch<F extends FetchLike>(nativeFetch: F, report: (message: string) => void = toastError): F & { __urbanLensWrapped: true } {
    const call = function (this: unknown, input: RequestInfo | URL, init?: FetchInit): Promise<Response> {
        const reported = init?.__ulReported === true;
        return nativeFetch.call(this, input, init).then(
            (response) => {
                if (!response.ok && !reported) report(response.status === 401 ? SESSION_ENDED_MESSAGE : `Request failed (HTTP ${response.status}).`);
                return response;
            },
            (err: unknown) => {
                const signal = init?.signal;
                const cancelled = signal?.aborted === true && !(signal.reason instanceof Error && signal.reason.name === "TimeoutError");
                if (!reported && !cancelled) report(isTimeout(err) ? "Request timed out." : "Network request failed.");
                throw err;
            },
        );
    };
    // Carries over any static members the runtime hangs on fetch.
    return Object.assign(call, nativeFetch, { __urbanLensWrapped: true as const });
}

export function installRequestErrorToasts(): void {
    document.addEventListener("htmx:responseError", (event) => {
        const xhr = (event as CustomEvent<{ xhr?: XMLHttpRequest }>).detail?.xhr;
        toastError(responseErrorMessage(xhr?.status, xhr?.responseText));
    });
    document.addEventListener("htmx:sendError", () => toastError("Network error sending request."));
    document.addEventListener("htmx:timeout", () => toastError("Request timed out."));
    // No htmx:abort handler: hx-sync="this:replace" aborts a superseded request as a matter of course.

    const current: WrappedFetch = window.fetch;
    if (current && !current.__urbanLensWrapped) window.fetch = wrapFetch(current);
}

type Control = HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;

const isControl = (el: Element): el is Control => el instanceof HTMLInputElement || el instanceof HTMLSelectElement || el instanceof HTMLTextAreaElement;

let validationReportsInstalled = false;

/**
 * htmx drops a request whose form fails its constraints without a word, so a change-triggered save vanishes. The field
 * being typed in shows its own complaint; one elsewhere is named in a toast, since reporting it would pull focus away.
 */
export function installValidationReports(): void {
    if (validationReportsInstalled) return;
    validationReportsInstalled = true;
    // The value each refused field was last toasted for, so an input-debounced form does not repeat it every pause.
    const toasted = new WeakMap<Element, string>();
    document.addEventListener("htmx:validation:halted", (event) => {
        const form = event.target instanceof HTMLFormElement ? event.target : null;
        const invalid = Array.from(form?.elements ?? []).find((el): el is Control => isControl(el) && el.willValidate && !el.validity.valid);
        if (!invalid) return;
        if (invalid === document.activeElement) {
            invalid.reportValidity();
            return;
        }
        if (toasted.get(invalid) === invalid.value) return;
        toasted.set(invalid, invalid.value);
        const label = invalid.labels?.[0]?.textContent?.trim() || invalid.name;
        toast.warning(`Not saved - ${label}: ${invalid.validationMessage}`);
    });
    document.addEventListener("htmx:beforeRequest", (event) => {
        const form = event.target instanceof Element ? event.target.closest("form") : null;
        for (const el of form?.elements ?? []) toasted.delete(el);
    });
}

/** Disables a form's submit buttons while its htmx request is in flight. */
export function installSubmitButtonLock(): void {
    const buttons = (target: EventTarget | null): Array<HTMLButtonElement | HTMLInputElement> => {
        const form = target instanceof HTMLFormElement ? target : target instanceof Element ? target.closest("form") : null;
        return form ? Array.from(form.querySelectorAll<HTMLButtonElement | HTMLInputElement>('.btn--submit, input[type="submit"]')) : [];
    };
    const lock = (target: EventTarget | null, busy: boolean): void => {
        for (const btn of buttons(target)) {
            btn.classList.toggle("is-loading", busy);
            btn.disabled = busy;
        }
    };
    document.addEventListener("htmx:beforeRequest", (e) => lock(e.target, true));
    document.addEventListener("htmx:afterRequest", (e) => lock(e.target, false));
}

const PREVIEW_INTERACTIVE = 'a, button, input[type="submit"], input[type="button"], [onclick], [hx-get], [hx-post], [hx-put], [hx-patch], [hx-delete]';

/**
 * While the owner previews their profile as someone else would see it, every control that would act is
 * inert, except the banner's own exit.
 */
export function installProfilePreviewGuard(): void {
    const guard = (e: Event): void => {
        if (!document.querySelector(".profile-preview-banner")) return;
        const el = e.target instanceof Element ? e.target.closest(PREVIEW_INTERACTIVE) : null;
        if (!el || el.closest(".profile-preview-banner")) return;
        e.preventDefault();
        e.stopImmediatePropagation();
        toast.warning("You're previewing your profile - exit preview to interact.");
    };
    document.addEventListener("click", guard, true);
    document.addEventListener("submit", guard, true);
}

/**
 * Floors an edit-in-place control at the size of the text it replaces, so the swap does not read as the
 * text shrinking. Call before the display element is emptied.
 */
export function sizeEditInPlaceInput(displayEl: Element, inputEl: HTMLElement): void {
    const rect = displayEl.getBoundingClientRect();
    if (rect.width > 0) inputEl.style.minWidth = `${rect.width}px`;
    if (rect.height > 0) inputEl.style.minHeight = `${rect.height}px`;
}

/** An HX-Trigger ``showToast`` payload. The message is text; a link to show after it travels separately. */
export interface TriggeredToast {
    level?: string;
    message?: string;
    link?: { label: string; href: string };
}

/** The path of a page on this site, or null for anything else. */
function sitePath(href: string): string | null {
    let url: URL;
    try {
        url = new URL(href, window.location.href);
    } catch {
        return null;
    }
    if (url.origin !== window.location.origin || (url.protocol !== "http:" && url.protocol !== "https:")) return null;
    return url.pathname + url.search + url.hash;
}

export function showTriggeredToast(detail: TriggeredToast | undefined): void {
    if (!detail) return;
    const level = toastLevel(detail.level);
    const message = detail.message ?? "";
    const href = detail.link ? sitePath(detail.link.href) : null;
    if (detail.link && href) toastWithAction(level, message, { label: detail.link.label, href });
    else toast[level](message);
}

export function installSiteRuntime(): void {
    installCsrfToken();
    configureToastr();
    installRequestErrorToasts();
    installSubmitButtonLock();
    installValidationReports();
    installProfilePreviewGuard();
    window.urbanlensSizeEditInPlaceInput = sizeEditInPlaceInput;
    document.addEventListener("showToast", (event) => showTriggeredToast((event as CustomEvent<TriggeredToast | undefined>).detail));
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", showServerMessages, { once: true });
    else showServerMessages();
}
