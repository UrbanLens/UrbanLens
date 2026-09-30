/**
 * The pin and wiki pages' floating actions toolbar (``partials/ui/_hierarchy_actions_fab.html``): collapsible per
 * user, with an undo button that mirrors the site's undo bar.
 */

import type { UndoState } from "./undo-bar";

function isUndoState(value: unknown): value is UndoState {
    return !!value && typeof value === "object" && typeof Reflect.get(value, "canUndo") === "boolean" && typeof Reflect.get(value, "label") === "string";
}

/** Returns an uninstaller for its document-level listeners. */
export function installActionsFab(fab: HTMLElement): () => void {
    const button = fab.querySelector<HTMLElement>("#pin-actions-fab-btn");
    const menu = fab.querySelector<HTMLElement>("#pin-actions-menu");
    const collapse = fab.querySelector<HTMLElement>("#pin-actions-collapse");
    const undo = fab.querySelector<HTMLButtonElement>("#pin-actions-undo");
    if (!button || !menu || !collapse) return () => undefined;
    const storageKey = `ul-pin-actions-expanded:${fab.dataset.user ?? ""}`;

    const apply = (open: boolean): void => {
        fab.classList.toggle("is-collapsed", !open);
        menu.hidden = !open;
        collapse.hidden = !open;
        button.hidden = open;
        try {
            localStorage.setItem(storageKey, open ? "1" : "0");
        } catch {
            // Storage unavailable: it opens expanded next time.
        }
    };
    let stored: string | null = null;
    try {
        stored = localStorage.getItem(storageKey);
    } catch {
        stored = null;
    }
    apply(stored !== "0");
    collapse.addEventListener("click", () => apply(false));
    button.addEventListener("click", () => apply(true));

    const mirror = (state: UndoState): void => {
        if (!undo) return;
        undo.disabled = !state.canUndo;
        undo.title = state.label;
        undo.setAttribute("data-tooltip", state.label);
    };
    const source = document.getElementById("ul-undo-btn");
    const sourceReady = source instanceof HTMLButtonElement && !source.hidden && !source.disabled;
    mirror({ canUndo: sourceReady, label: sourceReady ? (source.getAttribute("aria-label") ?? "Undo") : "Undo" });
    const onUndoState = (event: Event): void => {
        if (event instanceof CustomEvent && isUndoState(event.detail)) mirror(event.detail);
    };
    document.addEventListener("ul:undo-state", onUndoState);
    undo?.addEventListener("click", () => {
        if (!undo.disabled) document.getElementById("ul-undo-btn")?.click();
    });

    fab.querySelector("#pin-actions-hidden-sections")?.addEventListener("click", () => document.getElementById("tools-fab-btn")?.click());

    const showArticleActions = (tab: unknown): void => {
        for (const item of fab.querySelectorAll<HTMLElement>("[data-article-page-action]")) item.hidden = tab !== "article";
    };
    const onTabShown = (event: Event): void => showArticleActions(event instanceof CustomEvent && event.detail ? Reflect.get(event.detail, "tab") : undefined);
    document.body.addEventListener("ul:tabShown", onTabShown);
    showArticleActions(document.querySelector<HTMLElement>("[data-page-tabs] [data-tab].is-active")?.dataset.tab ?? "overview");

    return () => {
        document.removeEventListener("ul:undo-state", onUndoState);
        document.body.removeEventListener("ul:tabShown", onTabShown);
    };
}
