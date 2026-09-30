/**
 * ``data-comment-map-expand="<id>"`` opens that attached map in its viewer dialog (``static/js/comment-map.js``).
 */

function onClick(event: MouseEvent): void {
    const control = event.target instanceof Element ? event.target.closest<HTMLElement>("[data-comment-map-expand]") : null;
    const id = control?.dataset.commentMapExpand;
    if (id) window._expandCommentMap?.(id);
}

let installed = false;

export function installGlobalCommentMapExpand(): void {
    if (installed) return;
    installed = true;
    document.addEventListener("click", onClick);
}
