/**
 * Warns before leaving a page that has unsaved or still-saving changes.
 *
 * Pages with auto-saving forms (settings, site admin, setup, profile edit, safety
 * check-ins) report their state through ``window.autosaveGuard``: ``markDirty`` when
 * an edit happens, ``saveStarted``/``saveFinished`` around each request. The guard
 * then intercepts the three ways off a page - closing the tab, an HTMX request, and
 * an ordinary link click.
 *
 * The in-flight counter matters as much as the dirty flag: an auto-save that has
 * fired but not returned looks clean to the form while its data is still in transit.
 *
 * Ported out of ``base.html``'s inline script unchanged.
 */

let dirty = false;
let inFlight = 0;
let message = "Changes are still saving. Leave this page anyway?";

export interface AutosaveGuard {
    markDirty(): void;
    markClean(): void;
    saveStarted(): void;
    saveFinished(): void;
    /** Clear both signals so a deliberate navigation is not challenged again. */
    allowNavigation(): void;
    isBlocked(): boolean;
    setMessage(msg: string): void;
}

export const autosaveGuard: AutosaveGuard = {
    markDirty: () => {
        dirty = true;
    },
    markClean: () => {
        dirty = false;
    },
    saveStarted: () => {
        inFlight += 1;
    },
    // Floored at zero: an unmatched saveFinished would otherwise drive the count
    // negative and permanently disarm the guard.
    saveFinished: () => {
        inFlight = Math.max(0, inFlight - 1);
    },
    allowNavigation: () => {
        dirty = false;
        inFlight = 0;
    },
    isBlocked: () => dirty || inFlight > 0,
    setMessage: (msg: string) => {
        message = msg;
    },
};

/** Ask to leave, falling back to the native prompt if core.js has not loaded. */
function askToLeave(): Promise<boolean | "alt"> {
    if (window.confirmDialog) {
        return window.confirmDialog({
            title: "Leave page?",
            message,
            confirmLabel: "Leave anyway",
            danger: true,
        });
    }
    return Promise.resolve(window.confirm(message));
}

function isBlocked(): boolean {
    // Via the global so a page that replaces window.autosaveGuard is still honoured.
    return window.autosaveGuard ? window.autosaveGuard.isBlocked() : autosaveGuard.isBlocked();
}

function allowNavigation(): void {
    (window.autosaveGuard ?? autosaveGuard).allowNavigation();
}

function onBeforeUnload(event: BeforeUnloadEvent): void {
    if (!isBlocked()) return;
    // Browsers show their own wording here; the message is only used by our dialogs.
    event.preventDefault();
    event.returnValue = "";
}

interface HtmxConfirmEvent extends Event {
    detail: { issueRequest(skipConfirmation: boolean): void };
}

function onHtmxConfirm(event: Event): void {
    if (!isBlocked()) return;
    const confirmEvent = event as HtmxConfirmEvent;
    confirmEvent.preventDefault();
    void askToLeave().then((ok) => {
        if (!ok) return;
        allowNavigation();
        confirmEvent.detail.issueRequest(true);
    });
}

function onLinkClick(event: MouseEvent): void {
    if (!isBlocked()) return;
    // A modified click opens a new tab, window, or download and leaves this page
    // exactly where it is - there is nothing to warn about. Intercepting it would
    // also be actively harmful: the confirm path navigates via location.href, which
    // would hijack the current tab and discard the changes being guarded.
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;

    const target = event.target as HTMLElement | null;
    const link = target?.closest?.("a[href]");
    if (!(link instanceof HTMLAnchorElement)) return;

    const href = link.getAttribute("href");
    const scheme = href ? href.trim().toLowerCase() : "";
    // In-page anchors do not leave, script/data urls are not navigations worth
    // guarding, and a new tab leaves this page untouched.
    if (!href || href.charAt(0) === "#" || scheme.startsWith("javascript:") || scheme.startsWith("data:") || scheme.startsWith("vbscript:") || link.target === "_blank") return;

    event.preventDefault();
    void askToLeave().then((ok) => {
        if (!ok) return;
        allowNavigation();
        window.location.href = link.href;
    });
}

/** Reset module state. Test-only. */
export function resetAutosaveGuardForTests(): void {
    dirty = false;
    inFlight = 0;
    message = "Changes are still saving. Leave this page anyway?";
}

declare global {
    interface Window {
        autosaveGuard?: AutosaveGuard;
    }
}

export function installGlobalAutosaveGuard(): void {
    window.autosaveGuard = autosaveGuard;

    window.addEventListener("beforeunload", onBeforeUnload);
    // Capture phase: this has to win before a page's own click handlers act on it.
    document.addEventListener("click", onLinkClick, true);

    // htmx:confirm is bound on <body>, as it was inline, so it keeps running after
    // any handler a page binds on body itself. This bundle loads from <head> where
    // document.body is still null, hence the wait.
    const bindBody = (): void => document.body.addEventListener("htmx:confirm", onHtmxConfirm);
    if (document.body) bindBody();
    else document.addEventListener("DOMContentLoaded", bindBody);
}
