/**
 * Warns before leaving a page that has unsaved or still-saving changes.
 */

import { type LeaveConfirmationHandle, installLeaveConfirmation } from "./leave-confirmation";

let dirty = false;
let inFlight = 0;
let message = "Changes are still saving. Leave this page anyway?";
let leaveConfirmation: LeaveConfirmationHandle | null = null;

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

export function isBlocked(): boolean {
    // Via the global so a page that replaces window.autosaveGuard is still honoured.
    return window.autosaveGuard ? window.autosaveGuard.isBlocked() : autosaveGuard.isBlocked();
}

function allowNavigation(): void {
    (window.autosaveGuard ?? autosaveGuard).allowNavigation();
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

/** Reset module state. Test-only. */
export function resetAutosaveGuardForTests(): void {
    dirty = false;
    inFlight = 0;
    message = "Changes are still saving. Leave this page anyway?";
    leaveConfirmation?.resetForTests();
}

declare global {
    interface Window {
        autosaveGuard?: AutosaveGuard;
    }
}

export function installGlobalAutosaveGuard(): void {
    window.autosaveGuard = autosaveGuard;

    // beforeunload and link clicks are the same problem three pages have; only the
    // condition and the wording differ. See shared/leave-confirmation.ts.
    leaveConfirmation = installLeaveConfirmation({
        isBlocked,
        message: () => message,
        onConfirmed: allowNavigation,
    });

    // htmx:confirm is specific to this guard - the other two pages have no auto-saving requests to hold back.
    const bindBody = (): void => document.body.addEventListener("htmx:confirm", onHtmxConfirm);
    if (document.body) bindBody();
    else document.addEventListener("DOMContentLoaded", bindBody);
}
