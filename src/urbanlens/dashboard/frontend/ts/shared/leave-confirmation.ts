/**
 * "Are you sure you want to leave?" for pages with something worth losing.
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
 */
    uninstall(): void;
}

export function installLeaveConfirmation(options: LeaveConfirmationOptions): LeaveConfirmationHandle {
    // Set once the user has agreed to leave, and deliberately never cleared: the page is on its way out.
    //
    // The cost is that a confirmed click which somehow does not navigate leaves the page unguarded.
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
            // A modified or non-primary click opens a new tab, window, or download and leaves this page untouched, so there is nothing to warn about.
            if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;

            const target = event.target as HTMLElement | null;
            const link = target?.closest?.("a[href]");
            if (!(link instanceof HTMLAnchorElement)) return;

            const href = link.getAttribute("href");
            const scheme = href ? href.trim().toLowerCase() : "";
            // In-page anchors do not leave; script and data urls are not navigations worth guarding; a new tab leaves this page open.
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
