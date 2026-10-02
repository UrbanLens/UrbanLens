/**
 * One click listener for a set of named actions: a control carries ``data-<attribute>="<name>"`` and the page says
 * what each name does. A name outside the set does nothing, so markup reaches only what the page listed.
 */

export type DelegatedAction = (control: HTMLElement, event: MouseEvent) => void;

/**
 * Listen on *root* for clicks on controls naming one of *actions*.
 *
 * Args:
 *     root: Where to listen; an element answers only for controls inside it.
 *     attribute: The data attribute, without ``data-``.
 *     actions: What each name does.
 *
 * Returns:
 *     A function that removes the listener.
 */
export function delegateActions(root: Document | HTMLElement, attribute: string, actions: Record<string, DelegatedAction>): () => void {
    const name = `data-${attribute}`;
    const onClick = (event: Event): void => {
        if (!(event instanceof MouseEvent) || !(event.target instanceof Element)) return;
        const control = event.target.closest<HTMLElement>(`[${name}]`);
        if (!control || (root instanceof HTMLElement && !root.contains(control))) return;
        const action = control.getAttribute(name) ?? "";
        if (Object.hasOwn(actions, action)) actions[action]?.(control, event);
    };
    root.addEventListener("click", onClick);
    return () => root.removeEventListener("click", onClick);
}
