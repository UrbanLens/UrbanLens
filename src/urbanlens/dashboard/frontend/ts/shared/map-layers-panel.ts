/**
 * The layers strip's DOM half - flyout panel, button wiring, active-state syncing - with no tile
 * layer or map engine in it, so the Leaflet engine (`map-layers.ts`) and the MapLibre one
 * (`maplibre-layers.ts`) drive the same panel instead of keeping two copies of it in lockstep by
 * hand (the drift `P92` closed out for the pin-cluster badges).
 */

import type { BaseLayerKey, CustomLayerToggle } from "./map-layers";

/** The engine state the strip's buttons highlight from, in engine-free terms. */
export interface LayersButtonView {
    base: BaseLayerKey;
    weather: boolean;
    borders: boolean;
    dark: boolean;
    custom: Record<string, CustomLayerToggle>;
}

/** Engine entry points a button click routes to. */
export interface LayersPanelHandlers {
    toggleBase: (key: string) => void;
    toggleWeather: () => void;
    toggleBorders: () => void;
    toggleDark: () => void;
    toggleCustom: (key: string) => void;
}

export interface LayersPanel {
    open: () => void;
    close: () => void;
    toggle: () => void;
    isOpen: () => boolean;
    /** Re-applies every button's `.active` class from the engine's current state. */
    sync: (view: LayersButtonView) => void;
    /** Releases the document-level listener this panel registered. */
    destroy: () => void;
}

const PANEL_TRANSITION_MS = 220;

/**
 * Binds the rendered layers strip to an engine.
 * @param root - Strip root element; a null root yields a live instance whose methods are all no-ops, so an engine driving a map with no panel needs no special-casing.
 * @param hasWeather - False hides the weather button outright (no OpenWeatherMap key configured, so the feature cannot work).
 * @param handlers - Engine callbacks a button click routes to.
 */
export function createLayersPanel(root: HTMLElement | null, hasWeather: boolean, handlers: LayersPanelHandlers): LayersPanel {
    const toggleBtn = root?.querySelector<HTMLElement>("[data-layers-toggle]") ?? null;
    const menu = root?.querySelector<HTMLElement>("[data-layers-menu]") ?? null;
    let panelCloseTimer: ReturnType<typeof setTimeout> | null = null;

    function layerButton(key: string): HTMLElement | null {
        return root?.querySelector<HTMLElement>(`[data-map-layer="${key}"]`) ?? null;
    }

    function sync(view: LayersButtonView): void {
        if (!root) return;
        layerButton("street")?.classList.toggle("active", view.base === "street");
        layerButton("terrain")?.classList.toggle("active", view.base === "topographic");
        layerButton("satellite")?.classList.toggle("active", view.base === "satellite");
        layerButton("weather")?.classList.toggle("active", view.weather);
        layerButton("borders")?.classList.toggle("active", view.borders);
        layerButton("dark")?.classList.toggle("active", view.dark);
        for (const [key, toggle] of Object.entries(view.custom)) {
            const active = toggle.activeWhenOff ? !toggle.isActive() : toggle.isActive();
            layerButton(key)?.classList.toggle("active", active);
        }
    }

    function isOpen(): boolean {
        return root?.classList.contains("is-open") ?? false;
    }

    function close(): void {
        if (!root || !root.classList.contains("is-open")) return;
        root.classList.remove("is-open");
        if (toggleBtn) {
            toggleBtn.classList.remove("active");
            toggleBtn.setAttribute("aria-expanded", "false");
        }
        if (menu) {
            menu.setAttribute("aria-hidden", "true");
            let closed = false;
            const finishClose = (e?: Event) => {
                if (e && e.target !== menu) return;
                if (closed || root.classList.contains("is-open")) return;
                closed = true;
                if (panelCloseTimer) {
                    clearTimeout(panelCloseTimer);
                    panelCloseTimer = null;
                }
                menu.hidden = true;
                menu.removeEventListener("transitionend", finishClose);
            };
            menu.addEventListener("transitionend", finishClose);
            panelCloseTimer = setTimeout(finishClose, PANEL_TRANSITION_MS + 40);
        }
    }

    function open(): void {
        if (!root) return;
        if (panelCloseTimer) {
            clearTimeout(panelCloseTimer);
            panelCloseTimer = null;
        }
        if (menu) {
            menu.hidden = false;
            menu.setAttribute("aria-hidden", "false");
            // Force a synchronous reflow so the opening transition plays from
            // the hidden state instead of snapping.
            void menu.offsetWidth;
        }
        root.classList.add("is-open");
        if (toggleBtn) {
            toggleBtn.classList.add("active");
            toggleBtn.setAttribute("aria-expanded", "true");
        }
    }

    function toggle(): void {
        if (isOpen()) close();
        else open();
    }

    const onDocumentClick = (e: MouseEvent): void => {
        if (root && !root.contains(e.target as Node)) close();
    };
    if (toggleBtn) {
        toggleBtn.addEventListener("click", toggle);
        document.addEventListener("click", onDocumentClick);
    }

    if (root) {
        root.querySelectorAll<HTMLElement>("[data-map-layer]").forEach((btn) => {
            const key = btn.dataset.mapLayer!;
            const kind = btn.dataset.layerKind || "custom";
            if (key === "weather" && !hasWeather) {
                // No API key configured - the feature can't work, so don't offer it.
                btn.hidden = true;
                return;
            }
            btn.addEventListener("click", () => {
                if (kind === "base") handlers.toggleBase(key === "terrain" ? "topographic" : key);
                else if (key === "weather") handlers.toggleWeather();
                else if (key === "borders") handlers.toggleBorders();
                else if (key === "dark") handlers.toggleDark();
                else handlers.toggleCustom(key);
            });
        });
    }

    return {
        open,
        close,
        toggle,
        isOpen,
        sync,
        destroy: () => {
            if (toggleBtn) {
                toggleBtn.removeEventListener("click", toggle);
                document.removeEventListener("click", onDocumentClick);
            }
            if (panelCloseTimer) {
                clearTimeout(panelCloseTimer);
                panelCloseTimer = null;
            }
        },
    };
}
