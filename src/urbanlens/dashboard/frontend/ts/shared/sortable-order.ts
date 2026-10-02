/**
 * Saving the order a drag leaves a Sortable list in.
 */

/**
 * The page shows a dropped item in its new place at once, so when the newest save fails the list goes back to the
 * newest order a save landed. An older save failing changes nothing: the newer one decides what the list shows.
 * @param list - The sortable list.
 * @param save - Sends the order the list now shows.
 * @param onFailed - Tells the user the save failed.
 * @returns Sortable's onChoose and onEnd options.
 */
export function orderSaveHandlers(list: HTMLElement, save: () => Promise<unknown>, onFailed: (error: unknown) => void): { onChoose: () => void; onEnd: () => void } {
    let confirmed: Element[] = [];
    let confirmedAttempt = 0;
    let latest = 0;
    let latestFailed = false;
    return {
        // Before a touch drag floats its copy of the item in the list. The order the page loaded with is the server's.
        onChoose: () => {
            if (latest === 0) confirmed = Array.from(list.children);
        },
        onEnd: () => {
            const attempt = ++latest;
            latestFailed = false;
            const sent = Array.from(list.children);
            save().then(
                () => {
                    if (attempt < confirmedAttempt) return;
                    confirmed = sent;
                    confirmedAttempt = attempt;
                    if (latestFailed) list.append(...sent);
                },
                (error: unknown) => {
                    if (attempt === latest) {
                        latestFailed = true;
                        list.append(...confirmed);
                    }
                    onFailed(error);
                },
            );
        },
    };
}
