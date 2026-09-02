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

/** Fetch the overlay's body exactly once. Focuses the composer once it lands - on a first
 * open the composer doesn't exist yet at showModal() time, so openAssistantOverlay's own
 * focus attempt would silently find nothing without this. */
function loadBodyOnce(dlg: HTMLDialogElement): void {
    if (bodyLoaded) return;
    const url = dlg.dataset.overlayUrl;
    if (!url || !window.htmx) return;
    bodyLoaded = true;
    const onSwap = (event: Event): void => {
        if ((event.target as HTMLElement | null)?.id !== "assistant-overlay-body") return;
        document.body.removeEventListener("htmx:afterSwap", onSwap);
        focusComposer(dlg);
    };
    document.body.addEventListener("htmx:afterSwap", onSwap);
    window.htmx.ajax("GET", url, { target: "#assistant-overlay-body", swap: "innerHTML" });
}

export function openAssistantOverlay(): void {
    const dlg = dialog();
    if (!dlg) return;
    const alreadyLoaded = bodyLoaded;
    loadBodyOnce(dlg);
    if (!dlg.open) dlg.showModal();
    // On a first open, loadBodyOnce's own htmx:afterSwap listener focuses the
    // composer once it actually exists; focusing here too would just find
    // nothing (the body is still the loading skeleton) and no-op.
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
 * Turn a resolved turn's client_actions (HX-Trigger ulAssistantAction, see
 * controllers.assistant.AssistantTurnPollView) into the same document events
 * _page_explainer_script.html and onboarding-tour.ts already listen for -
 * reopening something not on the current page is a silent no-op there, not
 * an error here.
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
    installed = false;
    document.removeEventListener("keydown", onKeydown);
    document.removeEventListener("click", onClick);
    window.removeEventListener("resize", placeFab);
    document.body.removeEventListener("ulAssistantAction", onAssistantAction);
}

export function installGlobalAssistantOverlay(): void {
    if (installed) return;
    installed = true;
    document.addEventListener("keydown", onKeydown);
    document.addEventListener("click", onClick);
    window.addEventListener("resize", placeFab);
    document.body.addEventListener("ulAssistantAction", onAssistantAction);
    placeFab();
}
