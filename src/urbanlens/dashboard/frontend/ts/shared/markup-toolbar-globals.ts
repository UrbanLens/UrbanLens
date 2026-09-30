/**
 * The markup toolbar under the global names the map toolbar's buttons and ``_markup_panel_dialog.html`` call. Kept apart from
 * ``markup-toolbar.ts`` so a bundle that reaches the toolbar through ``window.createMarkupToolbar`` does not also carry a copy.
 */

import type { MarkupItem, MarkupToolbar } from "./markup-toolbar";

export function exposeMarkupToolbar(toolbar: MarkupToolbar): void {
    window.startMarkupDraw = toolbar.startMarkupDraw;
    window.startShapeDraw = toolbar.startShapeDraw;
    window.startTextPlacement = toolbar.startTextPlacement;
    window.closeMarkupPanel = toolbar.closeMarkupPanel;
    window._liveApplyMarkupEdit = toolbar.liveApplyMarkupEdit;
    window._closeMarkupDraw = toolbar.closeOrFinishDraw;
    window.deleteMarkupEdit = toolbar.deleteMarkupEdit;
    window.openMarkupEditDialog = toolbar.openMarkupEditDialog;
    window.loadMarkup = toolbar.loadMarkup;
}

declare global {
    interface Window {
        startMarkupDraw: (type: string) => void;
        startShapeDraw: (type: string) => void;
        startTextPlacement: () => void;
        closeMarkupPanel: () => void;
        _closeMarkupDraw: () => void;
        deleteMarkupEdit: () => Promise<void>;
        openMarkupEditDialog: (item: MarkupItem) => void;
        loadMarkup: () => void;
        _liveApplyMarkupEdit: () => void;
    }
}
