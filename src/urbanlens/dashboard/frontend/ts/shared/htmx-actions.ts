/**
 * Declarative request-lifecycle actions, in place of `hx-on` (which needs `'unsafe-eval'`).
 *
 * An element opts in with space-separated `name` or `name:argument` tokens:
 *
 * - `data-ul-before-request` - run as the request starts.
 * - `data-ul-after-request` - run when it ends, whatever the outcome.
 * - `data-ul-on-success` - run when it ends successfully, narrowed by `data-ul-success-status`
 *   and `data-ul-success-verb` where present. `data-ul-success-toast` adds a success toast.
 *
 * Like `hx-on`, the element sees requests issued by itself or any descendant, with itself as the
 * action's element. Listeners are bound to the element when a request first passes through it,
 * so they survive a swap that detaches it, which is when htmx stops bubbling to the document.
 *
 * `data-ul-min-query="N"` on a requesting input skips requests whose value is 1 to N-1
 * characters long; an empty value still goes through, so clearing a search resets it.
 *
 * `hx-confirm` asks in the site's confirm dialog, titled and labelled by `data-confirm-title` and
 * `data-confirm-label` beside it.
 */

import { confirmAction, toast } from "./dialogs";

export interface HtmxRequestDetail {
    elt?: Element;
    xhr?: XMLHttpRequest;
    successful?: boolean;
    requestConfig?: { verb?: string };
    /** On ``htmx:confirm``: the event that triggered the request. */
    triggeringEvent?: Event;
}

export type HtmxAction = (element: HTMLElement, event: CustomEvent<HtmxRequestDetail>, argument: string) => void;

const ATTRIBUTES = {
    before: "data-ul-before-request",
    after: "data-ul-after-request",
    success: "data-ul-on-success",
} as const;

const SELECTOR = `[${ATTRIBUTES.after}], [${ATTRIBUTES.success}], [data-ul-success-toast]`;

const actions = new Map<string, HtmxAction>();
const bound = new WeakSet<Element>();
/** The dialog each requesting element was in when its request started; a response can swap it out of the dialog. */
const startedIn = new WeakMap<Element, HTMLDialogElement>();

function byId(id: string): HTMLElement | null {
    return id ? document.getElementById(id) : null;
}

const BUILTIN_ACTIONS: Record<string, HtmxAction> = {
    "close-dialog": (el) => (el.closest("dialog") ?? startedIn.get(el))?.close(),
    reset: (el) => {
        if (el instanceof HTMLFormElement) el.reset();
    },
    "show-modal": (_el, _event, id) => {
        const dialog = byId(id);
        if (dialog instanceof HTMLDialogElement && !dialog.open) dialog.showModal();
    },
    close: (_el, _event, id) => {
        const dialog = byId(id);
        if (dialog instanceof HTMLDialogElement) dialog.close();
    },
    remove: (_el, _event, id) => byId(id)?.remove(),
    hide: (_el, _event, id) => {
        const target = byId(id);
        if (target) target.hidden = true;
    },
    "clear-field": (el, _event, name) => {
        const field = el.querySelector<HTMLInputElement | HTMLTextAreaElement>(`[name="${CSS.escape(name)}"]`);
        if (field) field.value = "";
    },
    "mark-deleting": (el) => el.closest("li")?.classList.add("is-deleting"),
    "scroll-bottom": (_el, _event, id) => {
        const target = byId(id);
        if (target) target.scrollTop = target.scrollHeight;
    },
    dispatch: (_el, _event, name) => {
        if (name) document.body.dispatchEvent(new Event(name));
    },
    redirect: (_el, _event, url) => {
        if (url.startsWith("/") && !url.startsWith("//")) window.location.assign(url);
    },
    "redirect-json": (_el, event) => {
        let target: unknown;
        try {
            target = (JSON.parse(event.detail.xhr?.responseText ?? "") as { redirect?: unknown }).redirect;
        } catch {
            return;
        }
        if (typeof target === "string" && target.startsWith("/") && !target.startsWith("//")) window.location.assign(target);
    },
};

export const BUILTIN_ACTION_NAMES: readonly string[] = Object.keys(BUILTIN_ACTIONS);

/**
 * Registers a named action for use in the `data-ul-*` request attributes.
 *
 * @param name - The token templates name it by.
 * @param action - Called with the declaring element, the htmx event and the token's argument.
 */
export function registerHtmxAction(name: string, action: HtmxAction): void {
    actions.set(name, action);
}

function runTokens(el: HTMLElement, value: string | null, event: CustomEvent<HtmxRequestDetail>): void {
    if (!value) return;
    for (const token of value.split(/\s+/)) {
        if (!token) continue;
        const colon = token.indexOf(":");
        const name = colon < 0 ? token : token.slice(0, colon);
        const argument = colon < 0 ? "" : token.slice(colon + 1);
        const action = actions.get(name);
        if (!action) {
            console.warn(`Unknown htmx action "${name}"`, el);
            continue;
        }
        action(el, event, argument);
    }
}

function succeeded(el: HTMLElement, detail: HtmxRequestDetail): boolean {
    if (!detail.successful) return false;
    const status = el.dataset.ulSuccessStatus;
    if (status && String(detail.xhr?.status) !== status) return false;
    const verb = el.dataset.ulSuccessVerb;
    if (verb && detail.requestConfig?.verb?.toLowerCase() !== verb.toLowerCase()) return false;
    return true;
}

function onAfterRequest(event: Event): void {
    const el = event.currentTarget;
    if (!(el instanceof HTMLElement)) return;
    const htmxEvent = event as CustomEvent<HtmxRequestDetail>;
    runTokens(el, el.getAttribute(ATTRIBUTES.after), htmxEvent);
    if (!succeeded(el, htmxEvent.detail ?? {})) return;
    runTokens(el, el.getAttribute(ATTRIBUTES.success), htmxEvent);
    const message = el.dataset.ulSuccessToast;
    if (message) toast.success(message);
}

function bind(el: Element): void {
    if (bound.has(el)) return;
    bound.add(el);
    el.addEventListener("htmx:afterRequest", onAfterRequest);
}

function onBeforeRequest(event: Event): void {
    const htmxEvent = event as CustomEvent<HtmxRequestDetail>;
    const origin = htmxEvent.target;
    if (!(origin instanceof Element)) return;
    for (let el: Element | null = origin; el; el = el.parentElement) {
        if (!(el instanceof HTMLElement)) continue;
        if (el.matches(SELECTOR)) {
            bind(el);
            const dialog = el.closest("dialog");
            if (dialog) startedIn.set(el, dialog);
            else startedIn.delete(el);
        }
        if (el.hasAttribute(ATTRIBUTES.before)) runTokens(el, el.getAttribute(ATTRIBUTES.before), htmxEvent);
    }
}

function onConfirm(event: Event): void {
    const elt = (event as CustomEvent<HtmxRequestDetail>).detail?.elt;
    if (!(elt instanceof HTMLInputElement || elt instanceof HTMLTextAreaElement)) return;
    const min = Number(elt.dataset.ulMinQuery);
    if (!min) return;
    const length = elt.value.length;
    if (length > 0 && length < min) event.preventDefault();
}

/**
 * A click on a ``data-own-click`` control (a button or link inside a clickable htmx element, such as a notification
 * row) does not also send the request of the element around it.
 */
function onOwnClick(event: Event): void {
    const detail: unknown = event instanceof CustomEvent ? event.detail : null;
    if (!detail || typeof detail !== "object") return;
    const elt: unknown = Reflect.get(detail, "elt");
    const triggering: unknown = Reflect.get(detail, "triggeringEvent");
    const clicked = triggering instanceof Event ? triggering.target : null;
    const control = clicked instanceof Element ? clicked.closest("[data-own-click]") : null;
    if (elt instanceof Element && control && control !== elt && elt.contains(control)) event.preventDefault();
}

/** An ``htmx:confirm`` detail's ``hx-confirm`` question, or null when the request has none. */
function hxQuestion(detail: unknown): string | null {
    const question: unknown = detail && typeof detail === "object" ? Reflect.get(detail, "question") : null;
    return typeof question === "string" && question ? question : null;
}

/**
 * Asks an ``htmx:confirm``'s ``hx-confirm`` question in the site's dialog; true when there is none. For a listener
 * that holds a request for its own reason and then sends it with ``issueRequest(true)``, which skips the question.
 */
export async function askHxQuestion(detail: unknown): Promise<boolean> {
    const question = hxQuestion(detail);
    if (!question) return true;
    const elt: unknown = detail && typeof detail === "object" ? Reflect.get(detail, "elt") : null;
    const asker = elt instanceof Element ? elt.closest<HTMLElement>("[hx-confirm]") : null;
    return confirmAction({ title: asker?.dataset.confirmTitle, message: question, confirmLabel: asker?.dataset.confirmLabel });
}

/** The request goes only on OK, through htmx's own ``issueRequest``. */
function onHxConfirm(event: Event): void {
    if (event.defaultPrevented || !(event instanceof CustomEvent)) return;
    const issueRequest: unknown = event.detail && typeof event.detail === "object" ? Reflect.get(event.detail, "issueRequest") : null;
    if (!hxQuestion(event.detail) || typeof issueRequest !== "function") return;
    event.preventDefault();
    void askHxQuestion(event.detail).then((ok) => {
        if (ok) Reflect.apply(issueRequest, undefined, [true]);
    });
}

/** Resets registrations to the built-ins. */
export function resetHtmxActions(): void {
    actions.clear();
    for (const [name, action] of Object.entries(BUILTIN_ACTIONS)) actions.set(name, action);
}

declare global {
    interface Window {
        ulHtmxActions?: { register: typeof registerHtmxAction };
    }
}

let installed = false;

export function installGlobalHtmxActions(): void {
    if (installed) return;
    installed = true;
    resetHtmxActions();
    window.ulHtmxActions = { register: registerHtmxAction };
    // Capture, so the listeners are bound before the event reaches the element.
    document.addEventListener("htmx:beforeRequest", onBeforeRequest, true);
    document.addEventListener("htmx:confirm", onConfirm);
    document.addEventListener("htmx:confirm", onOwnClick);
    // After onConfirm and onOwnClick, so a held-back request is never asked about.
    document.addEventListener("htmx:confirm", onHxConfirm);
}
