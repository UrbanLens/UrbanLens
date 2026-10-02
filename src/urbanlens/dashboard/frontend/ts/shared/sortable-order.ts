/**
 * Saving the order a drag leaves a Sortable list in.
 */

/**
 * The page shows a dropped item in its new place at once, so when the newest save fails the list goes back to the
 * last order a save landed. An older save failing changes nothing: the newer one decides what the list shows.
 * @param list - The sortable list.
 * @param save - Sends the order the list now shows.
 * @param onFailed - Tells the user the save failed.
 * @returns Sortable's onStart and onEnd options.
 */
export function orderSaveHandlers(list: HTMLElement, save: () => Promise<unknown>, onFailed: (error: unknown) => void): { onStart: () => void; onEnd: () => void } {
    let confirmed: Element[] = [];
    let latest = 0;
    return {
        onStart: () => {
            // The order the page loaded with is the server's, until a save says otherwise.
            if (latest === 0) confirmed = Array.from(list.children);
        },
        onEnd: () => {
            const attempt = ++latest;
            const sent = Array.from(list.children);
            save().then(
                () => {
                    confirmed = sent;
                },
                (error: unknown) => {
                    if (attempt === latest) list.append(...confirmed);
                    onFailed(error);
                },
            );
        },
    };
}
