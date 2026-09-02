/**
 * Global AI assistant overlay: a hotkey/floating-button-opened `<dialog>`
 * that reuses the same session-backed chat partials and endpoints
 * (_messages.html, _composer.html, /assistant/message/, /assistant/turn/...)
 * the full /assistant/ page renders - the overlay and the page are never two
 * separate implementations of the same conversation.
 *
 * Both the floating button and the dialog are rendered server-side only for
 * an enabled profile (see themes/base.html's `assistant_enabled_flag`), so
 * an ungated user has neither in the DOM - `fab()`/`dialog()` returning null
 * is this module's own signal that there is nothing to wire up here.
 *
 * The dialog's body starts as a loading skeleton and is fetched once, lazily,
 * the first time it opens - not embedded in every page's initial HTML, the
 * way the undo bar's contents are fetched async after mount rather than
 * rendered inline.
 */

import { positionAboveColliders } from "./floating-controls";
import { isTypingTarget, matchesHotkey } from "./hotkeys";

const FAB_COLLIDERS = [
    "#ul-undo-bar",
    ".map-buttons",
    ".floorplan-toolbar-stack",
    ".floorplan-canvas-controls",
    ".map-bottom-controls",
    ".article-floating-toolbar",
    ".ul-bulk-bar.visible",
    ".page-footer",
    "#toast-container",
];

let bodyLoaded = false;

function dialog(): HTMLDialogElement | null {
    return document.getElementById("assistant-overlay") as HTMLDialogElement | null;
}

function fab(): HTMLButtonElement | null {
    return document.getElementById("ul-assistant-fab") as HTMLButtonElement | null;
}

function placeFab(): void {
    const btn = fab();
    if (!btn || btn.hidden) return;
    positionAboveColliders(btn, "--ul-assistant-fab-offset-y", FAB_COLLIDERS);
}

function loadBodyOnce(dlg: HTMLDialogElement): void {
    if (bodyLoaded) return;
    const url = dlg.dataset.overlayUrl;
    if (!url || !window.htmx) return;
    bodyLoaded = true;
    window.htmx.ajax("GET", url, { target: "#assistant-overlay-body", swap: "innerHTML" });
}

export function openAssistantOverlay(): void {
    const dlg = dialog();
    if (!dlg) return;
    loadBodyOnce(dlg);
    if (!dlg.open) dlg.showModal();
    window.requestAnimationFrame(() => {
        dlg.querySelector<HTMLInputElement>('input[name="message"]')?.focus();
    });
}

function closeAssistantOverlay(): void {
    dialog()?.close();
}

function onKeydown(event: KeyboardEvent): void {
    if (isTypingTarget(event.target)) return;
    if (!matchesHotkey(event, "openAssistant")) return;
    if (!dialog()) return;
    event.preventDefault();
    openAssistantOverlay();
}

function onClick(event: MouseEvent): void {
    const target = event.target as HTMLElement | null;
    if (!target?.closest) return;
    if (target.closest("#ul-assistant-fab")) openAssistantOverlay();
    else if (target.closest("#assistant-overlay-close")) closeAssistantOverlay();
}

/** Reset module state. Test-only. */
export function resetAssistantOverlayForTests(): void {
    bodyLoaded = false;
    document.removeEventListener("keydown", onKeydown);
    document.removeEventListener("click", onClick);
    window.removeEventListener("resize", placeFab);
}

export function installGlobalAssistantOverlay(): void {
    document.addEventListener("keydown", onKeydown);
    document.addEventListener("click", onClick);
    window.addEventListener("resize", placeFab);
    placeFab();
}
