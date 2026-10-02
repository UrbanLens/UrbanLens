/**
 * The shared map toolbar (``partials/map/_map_toolbar.html``): each button's ``data-map-tool`` names what it does, and
 * the page that owns the map defines the function. A page without it ignores the click.
 */
import { delegateActions } from "./delegated-actions";

function screenshotContext(button: HTMLElement): unknown {
    try {
        return button.dataset.screenshotContext ? JSON.parse(button.dataset.screenshotContext) : null;
    } catch {
        return null;
    }
}

let installed = false;

export function installMapToolbarActions(): void {
    if (installed) return;
    installed = true;
    delegateActions(document, "map-tool", {
        "add-pin": () => window.openAddPinDialog?.(),
        "toggle-filter-panel": () => window.toggleFilterPanel?.(),
        "toggle-pin-list": () => window._togglePinListPanel?.(),
        "select-pins": () => window.toggleSelectMode?.(),
        "select-detail-pins": () => window.toggleDetailPinSelectMode?.(),
        "select-buildings": () => window.toggleBuildingImportSelectMode?.(),
        "pin-screenshot": () => window._openMapScreenshot?.(),
        screenshot: (button) => window._openMapToolbarScreenshot?.(window.map, screenshotContext(button)),
    });
}

declare global {
    interface Window {
        /** Defined by ``static/js/comment-map.js``, on every page. */
        _openMapToolbarScreenshot?: (map: unknown, context: unknown) => void;
    }
}
