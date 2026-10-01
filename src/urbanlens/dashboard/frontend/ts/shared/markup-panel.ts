/**
 * The draw/edit side panel (``partials/layout/_markup_panel_dialog.html``). Controls marked ``data-markup-live`` apply to
 * the item being edited as they change, and a ``data-readout`` names the element that shows a slider's value.
 */

export interface MarkupPanelActions {
    liveApply: () => void;
    closePanel: () => void;
    /** Finishes a valid in-progress shape, or just closes. */
    closeOrFinish: () => void;
    deleteEdit: () => Promise<void>;
}

export function wireMarkupPanel(panel: HTMLElement, actions: MarkupPanelActions): void {
    // The panel is a div, which the site's dialog-close handler leaves alone.
    panel.querySelector("[data-dialog-close]")?.addEventListener("click", actions.closePanel);
    panel.querySelector("[data-markup-close]")?.addEventListener("click", actions.closeOrFinish);
    panel.querySelector("[data-markup-delete]")?.addEventListener("click", () => void actions.deleteEdit());

    panel.addEventListener("input", (event) => {
        const target = event.target;
        // data-readout is shown by the core bundle (declarative-actions.ts).
        if (!(target instanceof HTMLInputElement) || !target.matches("[data-markup-live]")) return;
        actions.liveApply();
    });
    // A select fires input as well as change; change alone applies it once.
    panel.addEventListener("change", (event) => {
        if (event.target instanceof HTMLSelectElement && event.target.matches("[data-markup-live]")) actions.liveApply();
    });
}

export interface MarkupToolActions {
    startMarkupDraw: (type: string) => void;
    startShapeDraw: (type: string) => void;
    startTextPlacement: () => void;
}

const SHAPE_TOOLS = new Set(["square", "circle", "polygon"]);

/** Start the tool a map-toolbar button names in ``data-markup-tool``. Returns the uninstaller. */
export function wireMarkupTools(actions: MarkupToolActions): () => void {
    const onClick = (event: MouseEvent): void => {
        const tool = event.target instanceof Element ? event.target.closest<HTMLElement>("[data-markup-tool]")?.dataset.markupTool : undefined;
        if (!tool) return;
        if (tool === "text") actions.startTextPlacement();
        else if (SHAPE_TOOLS.has(tool)) actions.startShapeDraw(tool);
        else actions.startMarkupDraw(tool);
    };
    document.addEventListener("click", onClick);
    return () => document.removeEventListener("click", onClick);
}
