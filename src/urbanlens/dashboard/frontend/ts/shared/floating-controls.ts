/**
 * Shared bottom-right floating-control collision avoidance.
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
