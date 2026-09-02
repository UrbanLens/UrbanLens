/**
 * Shared bottom-right floating-control collision avoidance.
 *
 * Independent floating widgets (the undo bar, the assistant FAB, ...) each
 * want to sit bottom-right without overlapping whichever others are visible.
 * Each widget calls `positionAboveColliders` with its own root element, the
 * CSS custom property it lifts itself with, and the selectors of every
 * *other* bottom-right floating widget it should avoid. This is a
 * cross-register: undo-bar.ts's collider list includes the assistant FAB's
 * selector, and the assistant FAB's list includes the undo bar's, so
 * whichever renders lower always lifts the other regardless of DOM order or
 * which one mounted first.
 */

function inBottomRight(rect: DOMRect): boolean {
    return rect.right > window.innerWidth - 280 && rect.bottom > window.innerHeight - 220 && rect.width > 0 && rect.height > 0;
}

/** Set `root`'s `offsetProperty` custom property so it lifts above any visible `colliders` occupying the same bottom-right corner. */
export function positionAboveColliders(root: HTMLElement, offsetProperty: string, colliders: string[]): void {
    let offset = 0;
    for (const selector of colliders) {
        for (const node of document.querySelectorAll(selector)) {
            if (!(node instanceof HTMLElement) || node.hidden || node === root || root.contains(node)) continue;
            const rect = node.getBoundingClientRect();
            if (!inBottomRight(rect)) continue;
            const lift = window.innerHeight - rect.top + 8;
            if (lift > offset) offset = lift;
        }
    }
    root.style.setProperty(offsetProperty, `${offset}px`);
}
