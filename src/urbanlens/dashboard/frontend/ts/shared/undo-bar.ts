/**
 * Floating undo/redo buttons and Ctrl+Z / Ctrl+Shift+Z.
 *
 * The default provider talks to the server undo stack. A page can register a
 * local provider (the floorplan editor's in-memory history) that takes over
 * the same buttons while it is mounted.
 */

import { getCsrfToken } from "./csrf";
import { toast } from "./dialogs";
import { isTypingTarget, matchesHotkey } from "./hotkeys";

export interface UndoProvider {
    canUndo(): boolean;
    canRedo(): boolean;
    undoLabel?: () => string | null;
    redoLabel?: () => string | null;
    undo(): void | Promise<void>;
    redo(): void | Promise<void>;
}

export interface UndoStackState {
    can_undo: boolean;
    can_redo: boolean;
    undo_label: string | null;
    redo_label: string | null;
}

const COLLIDERS = [
    ".map-buttons",
    ".floorplan-toolbar-stack",
    ".floorplan-canvas-controls",
    ".map-bottom-controls",
    ".article-floating-toolbar",
    ".ul-bulk-bar.visible",
    ".page-footer",
    "#toast-container",
];

let localProvider: UndoProvider | null = null;
let serverState: UndoStackState = { can_undo: false, can_redo: false, undo_label: null, redo_label: null };
let installed = false;
let fetchWrapped = false;
let nativeFetch: typeof window.fetch | null = null;
let refreshTimer = 0;

function bar(): HTMLElement | null {
    return document.getElementById("ul-undo-bar");
}

function undoButton(): HTMLButtonElement | null {
    return document.getElementById("ul-undo-btn") as HTMLButtonElement | null;
}

function redoButton(): HTMLButtonElement | null {
    return document.getElementById("ul-redo-btn") as HTMLButtonElement | null;
}

function activeProvider(): UndoProvider {
    if (localProvider) return localProvider;
    return {
        canUndo: () => serverState.can_undo,
        canRedo: () => serverState.can_redo,
        undoLabel: () => serverState.undo_label,
        redoLabel: () => serverState.redo_label,
        undo: () => postStack("undo"),
        redo: () => postStack("redo"),
    };
}

function stackUrl(which: "stack" | "undo" | "redo"): string {
    const root = bar();
    if (!root) return "";
    if (which === "stack") return root.dataset.stackUrl ?? "";
    if (which === "undo") return root.dataset.undoUrl ?? "";
    return root.dataset.redoUrl ?? "";
}

async function fetchStack(): Promise<void> {
    const url = stackUrl("stack");
    if (!url || localProvider) {
        syncButtons();
        return;
    }
    try {
        const response = await fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } });
        if (!response.ok) return;
        const data = (await response.json()) as UndoStackState;
        serverState = {
            can_undo: !!data.can_undo,
            can_redo: !!data.can_redo,
            undo_label: data.undo_label ?? null,
            redo_label: data.redo_label ?? null,
        };
        syncButtons();
    } catch {
        /* offline - leave the last known state */
    }
}

function applyServerState(data: UndoStackState): void {
    serverState = {
        can_undo: !!data.can_undo,
        can_redo: !!data.can_redo,
        undo_label: data.undo_label ?? null,
        redo_label: data.redo_label ?? null,
    };
    syncButtons();
}

function flagPinsDirty(): void {
    try {
        localStorage.setItem("ul_pins_dirty", "1");
    } catch {
        /* unavailable */
    }
}

async function afterServerChange(): Promise<void> {
    flagPinsDirty();
    window.dispatchEvent(new CustomEvent("ul:undo-applied"));
    const refresh = window._refreshAllPins;
    if (typeof refresh === "function") {
        await refresh();
        return;
    }
    window.location.reload();
}

// Both the click handlers and the Ctrl+Z/Ctrl+Shift+Z shortcut funnel through
// this one function for the server-backed provider (see activeProvider below),
// so guarding here covers a rapid double-click and a held-down shortcut alike.
// The backend already serializes a genuine double-submit correctly (the loser
// gets a 410/"already restored" style error), but a same-tab double-click was
// still sending a second real request for what the user experienced as one
// action - which surfaced as a confusing error toast for a redundant click,
// not any actual corruption.
let requestInFlight = false;

async function postStack(which: "undo" | "redo"): Promise<void> {
    if (requestInFlight) return;
    const url = stackUrl(which);
    if (!url) return;
    requestInFlight = true;
    try {
        const response = await fetch(url, {
            method: "POST",
            headers: { "X-CSRFToken": getCsrfToken(), "X-Requested-With": "XMLHttpRequest" },
        });
        let data: UndoStackState & { ok?: boolean; error?: string } | null = null;
        try {
            data = (await response.json()) as UndoStackState & { ok?: boolean; error?: string };
        } catch {
            data = null;
        }
        if (data) applyServerState(data);
        if (!response.ok) {
            toast.error(data?.error || (which === "undo" ? "Could not undo." : "Could not redo."));
            return;
        }
        await afterServerChange();
    } finally {
        requestInFlight = false;
    }
}

function setHidden(node: HTMLElement | null, hidden: boolean): void {
    if (!node) return;
    node.hidden = hidden;
}

export function syncUndoBar(): void {
    syncButtons();
}

function syncButtons(): void {
    const provider = activeProvider();
    const canUndo = provider.canUndo();
    const canRedo = provider.canRedo();
    const undoBtn = undoButton();
    const redoBtn = redoButton();
    const root = bar();
    setHidden(undoBtn, !canUndo);
    setHidden(redoBtn, !canRedo);
    setHidden(root, !canUndo && !canRedo);
    if (undoBtn) {
        const label = provider.undoLabel?.() || "Undo";
        undoBtn.setAttribute("aria-label", `Undo: ${label}`);
        undoBtn.setAttribute("data-tooltip", `Undo (Ctrl+Z) · ${label}`);
        undoBtn.disabled = !canUndo;
    }
    if (redoBtn) {
        const label = provider.redoLabel?.() || "Redo";
        redoBtn.setAttribute("aria-label", `Redo: ${label}`);
        redoBtn.setAttribute("data-tooltip", `Redo (Ctrl+Shift+Z) · ${label}`);
        redoBtn.disabled = !canRedo;
    }
    placeBar();
}

function inBottomRight(rect: DOMRect): boolean {
    return rect.right > window.innerWidth - 280 && rect.bottom > window.innerHeight - 220 && rect.width > 0 && rect.height > 0;
}

function placeBar(): void {
    const root = bar();
    if (!root || root.hidden) return;
    let offset = 0;
    for (const selector of COLLIDERS) {
        for (const node of document.querySelectorAll(selector)) {
            if (!(node instanceof HTMLElement) || node.hidden || node === root || root.contains(node)) continue;
            const rect = node.getBoundingClientRect();
            if (!inBottomRight(rect)) continue;
            const lift = window.innerHeight - rect.top + 8;
            if (lift > offset) offset = lift;
        }
    }
    root.style.setProperty("--ul-undo-offset-y", `${offset}px`);
}

function onKeydown(event: KeyboardEvent): void {
    if (isTypingTarget(event.target)) return;
    const redo = matchesHotkey(event, "redo");
    if (!redo && !matchesHotkey(event, "undo")) return;
    const provider = activeProvider();
    if (redo) {
        if (!provider.canRedo()) return;
        event.preventDefault();
        void provider.redo();
        if (localProvider) syncButtons();
        return;
    }
    if (!provider.canUndo()) return;
    event.preventDefault();
    void provider.undo();
    if (localProvider) syncButtons();
}

function scheduleRefresh(): void {
    if (localProvider) return;
    window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(() => {
        void fetchStack();
    }, 50);
}

function onHtmxAfterRequest(event: Event): void {
    const detail = (event as CustomEvent).detail as { successful?: boolean; requestConfig?: { verb?: string } } | undefined;
    const verb = (detail?.requestConfig?.verb ?? "").toLowerCase();
    if (detail?.successful && verb && verb !== "get") scheduleRefresh();
}

function requestUrl(input: RequestInfo | URL): string {
    if (typeof input === "string") return input;
    if (input instanceof URL) return input.href;
    return input.url;
}

function requestMethod(input: RequestInfo | URL, init?: RequestInit): string {
    if (init?.method) return init.method.toUpperCase();
    if (typeof Request !== "undefined" && input instanceof Request) return input.method.toUpperCase();
    return "GET";
}

function wrapFetch(): void {
    if (fetchWrapped) return;
    fetchWrapped = true;
    nativeFetch = window.fetch.bind(window);
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
        const response = await (nativeFetch as typeof window.fetch)(input, init);
        const url = requestUrl(input);
        const isStackCall = url.includes("/undo/stack") || url.includes("/undo/undo") || url.includes("/undo/redo");
        const method = requestMethod(input, init);
        const mutating = method === "POST" || method === "PUT" || method === "PATCH" || method === "DELETE";
        if (response.ok && mutating && !isStackCall) scheduleRefresh();
        return response;
    };
}

export function resetUndoBarForTests(): void {
    localProvider = null;
    serverState = { can_undo: false, can_redo: false, undo_label: null, redo_label: null };
    installed = false;
    requestInFlight = false;
    window.clearTimeout(refreshTimer);
    document.removeEventListener("keydown", onKeydown);
    window.removeEventListener("resize", placeBar);
    document.body?.removeEventListener("htmx:afterRequest", onHtmxAfterRequest);
    if (fetchWrapped && nativeFetch) {
        window.fetch = nativeFetch;
        fetchWrapped = false;
        nativeFetch = null;
    }
    bar()?.remove();
}

function ensureBar(): HTMLElement {
    let root = bar();
    if (root) return root;
    root = document.createElement("nav");
    root.id = "ul-undo-bar";
    root.className = "ul-undo-bar";
    root.hidden = true;
    root.setAttribute("aria-label", "Undo and redo");
    root.innerHTML =
        `<button type="button" class="map-btn-icon" id="ul-undo-btn" hidden aria-label="Undo" data-tooltip="Undo (Ctrl+Z)" data-tooltip-float="true" data-tooltip-pos="above">` +
        `<i class="material-symbols-outlined">undo</i></button>` +
        `<button type="button" class="map-btn-icon" id="ul-redo-btn" hidden aria-label="Redo" data-tooltip="Redo (Ctrl+Shift+Z)" data-tooltip-float="true" data-tooltip-pos="above">` +
        `<i class="material-symbols-outlined">redo</i></button>`;
    document.body.appendChild(root);
    return root;
}

function bindButtons(): void {
    undoButton()?.addEventListener("click", () => {
        void activeProvider().undo();
        if (localProvider) syncButtons();
    });
    redoButton()?.addEventListener("click", () => {
        void activeProvider().redo();
        if (localProvider) syncButtons();
    });
}

export function registerLocalUndoProvider(provider: UndoProvider | null): void {
    localProvider = provider;
    syncButtons();
}

export function installUndoBar(): void {
    if (installed) return;
    installed = true;
    const bind = (): void => {
        ensureBar();
        bindButtons();
        document.addEventListener("keydown", onKeydown);
        wrapFetch();
        document.body.addEventListener("htmx:afterRequest", onHtmxAfterRequest);
        window.addEventListener("resize", placeBar);
        window.ulUndo = { register: registerLocalUndoProvider, sync: syncUndoBar };
        void fetchStack();
    };
    if (document.body) bind();
    else document.addEventListener("DOMContentLoaded", bind);
}

declare global {
    interface Window {
        _refreshAllPins?: () => Promise<void> | void;
        ulUndo?: {
            register: typeof registerLocalUndoProvider;
            sync: typeof syncUndoBar;
        };
    }
}
