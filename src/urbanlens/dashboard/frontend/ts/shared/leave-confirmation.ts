/**
 * "Are you sure you want to leave?" for pages with something worth losing.
 *
 * Three pages grew their own copy of this: the auto-save guard, the safety
 * check-in page (whose warning is that nobody would be notified if something
 * happened), and the tools page during an export. The copies had drifted and each
 * carried the same two defects, so the behaviour lives here once and each caller
 * supplies only what differs - when it is blocked, and what to say.
 *
 * Two ways off the page are covered: closing the tab, which only the browser's own
 * prompt can guard, and clicking an ordinary link, which is intercepted so the
 * question can be asked in the site's dialog instead.
 */

export interface LeaveConfirmationOptions {
    /** Whether there is currently something to warn about. Re-evaluated per event. */
    isBlocked(): boolean;
    /** A function when the wording depends on state at the time of asking. */
    message: string | (() => string);
    title?: string;
    confirmLabel?: string;
    /** Run once the user has agreed to go, before the navigation starts. */
    onConfirmed?(): void;
}

function resolveMessage(options: LeaveConfirmationOptions): string {
    return typeof options.message === "function" ? options.message() : options.message;
}

function ask(options: LeaveConfirmationOptions): Promise<boolean | "alt"> {
    const message = resolveMessage(options);
    if (window.confirmDialog) {
        return window.confirmDialog({
            title: options.title ?? "Leave page?",
            message,
            confirmLabel: options.confirmLabel ?? "Leave anyway",
            danger: true,
        });
    }
    // core.js has not loaded - the native prompt is worse but better than silence.
    return Promise.resolve(window.confirm(message));
}

export interface LeaveConfirmationHandle {
    /** Re-arm after a confirmed leave. Test-only. */
    resetForTests(): void;
    /**
     * Unbind both listeners.
     *
     * A page installs one guard and keeps it for its lifetime, so production
     * never calls this. Tests install one per case against a shared `document`
     * that is never torn down between them, and without a way to unbind, every
     * case leaves its capture-phase click listener behind for the rest of the
     * run.
     */
    uninstall(): void;
}

export function installLeaveConfirmation(options: LeaveConfirmationOptions): LeaveConfirmationHandle {
    // Set once the user has agreed to leave, and deliberately never cleared: the
    // page is on its way out. Without it the navigation we start ourselves re-enters
    // beforeunload with the page still blocked and the browser asks a second time,
    // so the user answers the same question twice.
    //
    // The cost is that a confirmed click which somehow does not navigate leaves the
    // page unguarded. Links that genuinely do not navigate are filtered out below
    // instead, which covers the realistic cases without guessing at a re-arm delay.
    let leaving = false;

    const blocked = (): boolean => !leaving && options.isBlocked();

    const onBeforeUnload = (event: BeforeUnloadEvent): void => {
        if (!blocked()) return;
        // Browsers show their own wording here; ours is only used in the dialog.
        event.preventDefault();
        event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);

    const onClick = (event: MouseEvent): void => {
            if (!blocked()) return;
            // A modified or non-primary click opens a new tab, window, or download
            // and leaves this page untouched, so there is nothing to warn about.
            // Intercepting it would also be harmful: the confirm path navigates the
            // current tab, discarding the very thing being guarded.
            if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;

            const target = event.target as HTMLElement | null;
            const link = target?.closest?.("a[href]");
            if (!(link instanceof HTMLAnchorElement)) return;

            const href = link.getAttribute("href");
            const scheme = href ? href.trim().toLowerCase() : "";
            // In-page anchors do not leave; script and data urls are not navigations
            // worth guarding; a new tab leaves this page open; and a download link
            // saves a file without navigating at all.
            if (
                !href ||
                href.charAt(0) === "#" ||
                scheme.startsWith("javascript:") ||
                scheme.startsWith("data:") ||
                scheme.startsWith("vbscript:") ||
                link.target === "_blank" ||
                link.hasAttribute("download")
            ) {
                return;
            }

            event.preventDefault();
            const destination = link.href;
            void ask(options).then((ok) => {
                if (!ok) return;
                leaving = true;
                options.onConfirmed?.();
                window.location.href = destination;
            });
    };
    // Capture phase: this has to win before a page's own click handlers act.
    document.addEventListener("click", onClick, true);

    return {
        resetForTests: () => {
            leaving = false;
        },
        uninstall: () => {
            window.removeEventListener("beforeunload", onBeforeUnload);
            document.removeEventListener("click", onClick, true);
        },
    };
}

declare global {
    interface Window {
        ulInstallLeaveConfirmation?: typeof installLeaveConfirmation;
    }
}

export function installGlobalLeaveConfirmation(): void {
    // Exposed for pages whose blocked-condition is written inline against
    // server-rendered state (the safety check-in page, the tools export page).
    window.ulInstallLeaveConfirmation = installLeaveConfirmation;
}
