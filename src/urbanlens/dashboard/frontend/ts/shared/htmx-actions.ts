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
 */

import { toast } from "./dialogs";

export interface HtmxRequestDetail {
    elt?: Element;
    xhr?: XMLHttpRequest;
    successful?: boolean;
    requestConfig?: { verb?: string };
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

function byId(id: string): HTMLElement | null {
    return id ? document.getElementById(id) : null;
}

const BUILTIN_ACTIONS: Record<string, HtmxAction> = {
    "close-dialog": (el) => el.closest("dialog")?.close(),
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

function onAfterRequest(this: HTMLElement, event: Event): void {
    const htmxEvent = event as CustomEvent<HtmxRequestDetail>;
    runTokens(this, this.getAttribute(ATTRIBUTES.after), htmxEvent);
    if (!succeeded(this, htmxEvent.detail ?? {})) return;
    runTokens(this, this.getAttribute(ATTRIBUTES.success), htmxEvent);
    const message = this.dataset.ulSuccessToast;
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
        if (el.matches(SELECTOR)) bind(el);
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
}
