/**
 * Global AI assistant overlay: a hotkey/floating-button-opened `<dialog>` that reuses the same session-backed chat partials.
 */

import { positionAboveColliders } from "./floating-controls";
import { isTypingTarget, matchesHotkey } from "./hotkeys";

const FAB_COLLIDERS = [
    "#ul-undo-bar",
    ".map-buttons",
    ".floorplan-toolbar-stack",
    ".floorplan-canvas-controls",
    ".map-bottom-controls",
    ".ul-bulk-bar.visible",
    ".page-footer",
    "#toast-container",
];

let bodyLoaded = false;
let bodyLoading = false;
let installed = false;

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

function focusComposer(dlg: HTMLDialogElement): void {
    window.requestAnimationFrame(() => {
        dlg.querySelector<HTMLInputElement>('input[name="message"]')?.focus();
    });
}

/**
 * Fetch the overlay's body exactly once.
 */
function loadBodyOnce(dlg: HTMLDialogElement): void {
    if (bodyLoaded || bodyLoading) return;
    const url = dlg.dataset.overlayUrl;
    if (!url || !window.htmx) return;
    bodyLoading = true;
    const cleanup = (): void => {
        bodyLoading = false;
        document.body.removeEventListener("htmx:afterSwap", onSwap);
        document.body.removeEventListener("htmx:responseError", onFailure);
        document.body.removeEventListener("htmx:sendError", onFailure);
        document.body.removeEventListener("htmx:timeout", onFailure);
    };
    const onSwap = (event: Event): void => {
        if ((event.target as HTMLElement | null)?.id !== "assistant-overlay-body") return;
        bodyLoaded = true;
        cleanup();
        focusComposer(dlg);
    };
    // These events don't reliably identify which in-flight request they belong to the way afterSwap's event.target does, so any failure.
    const onFailure = (): void => cleanup();
    document.body.addEventListener("htmx:afterSwap", onSwap);
    document.body.addEventListener("htmx:responseError", onFailure);
    document.body.addEventListener("htmx:sendError", onFailure);
    document.body.addEventListener("htmx:timeout", onFailure);
    window.htmx.ajax("GET", url, { target: "#assistant-overlay-body", swap: "innerHTML" });
}

export function openAssistantOverlay(): void {
    const dlg = dialog();
    if (!dlg) return;
    const alreadyLoaded = bodyLoaded;
    loadBodyOnce(dlg);
    if (!dlg.open) dlg.showModal();
    // On a first open, loadBodyOnce's own htmx:afterSwap listener focuses the composer once it actually exists.
    if (alreadyLoaded) focusComposer(dlg);
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

/** One reopen_explainer tool result, forwarded by the poll view's HX-Trigger - see controllers/assistant.py. */
interface AssistantClientAction {
    action: string;
    id?: string;
    kind?: "explainer" | "tour";
    prefix?: string;
}

/**
 * Turn a resolved turn's client_actions (HX-Trigger ulAssistantAction, see controllers.assistant.AssistantTurnPollView) into the same.
 */
function onAssistantAction(event: Event): void {
    const actions = (event as CustomEvent<{ actions?: AssistantClientAction[] }>).detail?.actions;
    if (!actions) return;
    for (const action of actions) {
        if (action.action !== "reopen_explainer") continue;
        if (action.kind === "explainer" && action.id) {
            document.dispatchEvent(new CustomEvent("ul:explainer-reopen", { detail: { id: action.id } }));
        } else if (action.kind === "tour" && action.prefix && action.id) {
            document.dispatchEvent(new CustomEvent("ul:tour-restart", { detail: { prefix: action.prefix, id: action.id } }));
        }
    }
}

/** Reset module state. Test-only. */
export function resetAssistantOverlayForTests(): void {
    bodyLoaded = false;
    bodyLoading = false;
    installed = false;
    document.removeEventListener("keydown", onKeydown);
    document.removeEventListener("click", onClick);
    window.removeEventListener("resize", placeFab);
    document.body?.removeEventListener("ulAssistantAction", onAssistantAction);
}

/** Run `bind` now if `<body>` is parsed, else as soon as it is. */
function whenBodyExists(bind: () => void): void {
    if (document.body) bind();
    else document.addEventListener("DOMContentLoaded", bind, { once: true });
}

export function installGlobalAssistantOverlay(): void {
    if (installed) return;
    installed = true;
    document.addEventListener("keydown", onKeydown);
    document.addEventListener("click", onClick);
    window.addEventListener("resize", placeFab);
    // This ships in the classic `core.js` bundle, which `themes/base.html` loads from `<head>`.
    whenBodyExists(() => {
        document.body.addEventListener("ulAssistantAction", onAssistantAction);
        placeFab();
    });
}
