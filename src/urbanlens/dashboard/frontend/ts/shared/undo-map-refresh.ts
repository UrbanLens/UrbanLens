/**
 * Flag the map's client pin cache dirty after an Undo History restore.
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
