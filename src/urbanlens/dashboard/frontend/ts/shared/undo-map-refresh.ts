/**
 * Flag the map's client pin cache dirty after an Undo History restore.
 *
 * The map keeps its pins in localStorage and its poll only compares the newest
 * pin's ``updated`` timestamp. A restored *pin* advances that and is picked up;
 * a restored *label* is not - its pin assignments come back through the M2M
 * without touching any pin row - and a restored list or markup map can likewise
 * change what the map should show without moving the poll's high-water mark. So
 * the same ``ul_pins_dirty`` flag every other non-map mutation surface sets
 * (``entries/organize.ts``, the delete flows in ``confirm-dialog.ts``) has to be
 * set here too, or the map keeps rendering the pre-restore world until its
 * cache naturally expires.
 *
 * Delegated from ``document.body`` and installed once by ``core.js`` rather than
 * inlined in the undo partial: the partial is re-swapped by HTMX after every
 * restore, so an inline listener would stack one copy per swap.
 */

function isRestoreRequest(detail: { successful?: boolean; requestConfig?: { verb?: string; path?: string } }): boolean {
    if (!detail.successful) return false;
    if ((detail.requestConfig?.verb ?? "").toLowerCase() !== "post") return false;
    const path = detail.requestConfig?.path ?? "";
    if (!path.includes("/undo/")) return false;
    return path.endsWith("/restore/") || path.endsWith("/undo/") || path.endsWith("/redo/");
}

function onAfterRequest(event: Event): void {
    const detail = (event as CustomEvent).detail as Parameters<typeof isRestoreRequest>[0] | undefined;
    if (!detail || !isRestoreRequest(detail)) return;
    try {
        localStorage.setItem("ul_pins_dirty", "1");
    } catch {
        /* localStorage unavailable - the map falls back to its periodic poll */
    }
}

export function installGlobalUndoMapRefresh(): void {
    // htmx:afterRequest bubbles to body; core.js runs from <head>, so wait for it.
    const bind = (): void => document.body.addEventListener("htmx:afterRequest", onAfterRequest);
    if (document.body) bind();
    else document.addEventListener("DOMContentLoaded", bind);
}

export { isRestoreRequest, onAfterRequest };
