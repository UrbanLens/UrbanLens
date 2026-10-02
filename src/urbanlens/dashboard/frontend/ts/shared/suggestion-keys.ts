/**
 * Keyboard access for a search box's suggestion list. Focus stays in the box: ArrowDown and ArrowUp move a highlight
 * through the visible suggestions, Enter picks the highlighted one (the first when none is), and Escape closes the
 * list rather than the dialog around it.
 */

/** Marks the highlighted suggestion; each list styles it as its hover state. */
export const ACTIVE_SUGGESTION_CLASS = "is-active";

export interface SuggestionList {
    /** The suggestions that can be picked right now, in display order. */
    items: () => HTMLElement[];
    isOpen: () => boolean;
    close: () => void;
}

/** One keydown in the search box, for a picker that delegates its events from the document. */
export function handleSuggestionKey(event: KeyboardEvent, { items, isOpen, close }: SuggestionList): void {
    if (!isOpen()) return;
    if (event.key === "Escape") {
        event.preventDefault();
        close();
        return;
    }
    const list = items();
    if (!list.length) return;
    const current = list.findIndex((item) => item.classList.contains(ACTIVE_SUGGESTION_CLASS));
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        const next = current < 0 ? (step > 0 ? 0 : list.length - 1) : (current + step + list.length) % list.length;
        list.forEach((item, index) => {
            item.classList.toggle(ACTIVE_SUGGESTION_CLASS, index === next);
            item.setAttribute("aria-selected", String(index === next));
        });
        list[next]?.scrollIntoView?.({ block: "nearest" });
    } else if (event.key === "Enter") {
        event.preventDefault();
        (list[current] ?? list[0])?.click();
    }
}

export function installSuggestionKeys({ input, ...list }: SuggestionList & { input: HTMLInputElement }): void {
    input.addEventListener("keydown", (event) => handleSuggestionKey(event, list));
}
