/**
 * The page-wide "child pin details" setting. The page states it once (``data-child-details``), each request that
 * depends on it says it explicitly, and the server announces a change with ``HX-Trigger`` (PinController.child_details).
 */

/** The event the server triggers when the setting changes; its detail is ``{include: boolean}``. */
export const CHILD_DETAILS_EVENT = "childDetailsChanged";

/**
 * A URL with the setting applied.
 * @param url - A same-origin path or an absolute URL; empty stays empty.
 * @param include - Whether to include the children, or null when the page has no setting and the URL is left alone.
 * @returns The URL with ``children`` set to 1 or 0, its other parameters kept.
 */
export function withChildDetails(url: string, include: boolean | null): string {
    if (!url || include === null) return url;
    const parsed = new URL(url, window.location.origin);
    parsed.searchParams.set("children", include ? "1" : "0");
    return parsed.origin === window.location.origin && !/^[a-z][a-z\d+.-]*:/i.test(url) ? parsed.pathname + parsed.search + parsed.hash : parsed.toString();
}

/**
 * The setting a page states on an element.
 * @param el - The element carrying ``data-child-details``.
 * @returns True or false, or null when the page does not state one.
 */
export function readChildDetails(el: HTMLElement): boolean | null {
    const value = el.dataset.childDetails;
    return value === undefined ? null : value === "1";
}

/**
 * The new setting a {@link CHILD_DETAILS_EVENT} carries.
 * @param event - The event.
 * @returns True or false, or null when it does not say.
 */
export function childDetailsChange(event: Event): boolean | null {
    const include = (event as CustomEvent<{ include?: unknown } | undefined>).detail?.include;
    return typeof include === "boolean" ? include : null;
}
