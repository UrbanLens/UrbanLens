/**
 * Bring the URL's target anchor into view once it exists.
 *
 * The browser's own fragment scrolling happens at load, which is too early for
 * anything HTMX fetches afterwards - a link to a specific comment lands on a page
 * where that comment has not been rendered yet. Re-running the scroll after each
 * settle is what makes those links work.
 *
 * Ported out of ``base.html``'s inline script unchanged.
 */

/**
 * Find the element a fragment refers to.
 *
 * ``getElementById`` is tried first because it takes a literal id, where
 * ``querySelector`` takes a selector and throws a ``DOMException`` on anything that
 * is not valid CSS. Plenty of real fragments are not: ids beginning with a digit,
 * and the ``#_=_`` / ``#access_token=...`` that OAuth providers append on the way
 * back from sign-in. Those threw on every HTMX settle for the life of the page.
 */
function findTarget(hash: string): Element | null {
    const raw = hash.slice(1);
    if (!raw) return null;

    let id = raw;
    try {
        id = decodeURIComponent(raw);
    } catch {
        /* a malformed % escape - fall back to the raw fragment */
    }

    const byId = document.getElementById(id);
    if (byId) return byId;

    // Kept for fragments that name something other than an id.
    try {
        return document.querySelector(hash);
    } catch {
        return null;
    }
}

// The hash last successfully scrolled to. Without this, every later
// htmx:afterSettle on the page - a pagination click, a like, an unrelated
// form submit - re-ran this and yanked the reader back to the original
// anchor, since the URL's hash stays set long after it did its job.
let scrolledToHash = "";

export function scrollToHash(): void {
    const hash = window.location.hash;
    if (!hash || hash === scrolledToHash) return;
    const target = findTarget(hash);
    if (!target) return; // not rendered yet - a later settle will retry
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    scrolledToHash = hash;
}

/** Reset the "already scrolled here" memory. Test-only: a real page navigation
 * already gets a fresh module instance. */
export function resetScrollToHashForTests(): void {
    scrolledToHash = "";
}

export function installGlobalScrollToHash(): void {
    document.addEventListener("htmx:afterSettle", scrollToHash);
    // The delay covers content that renders shortly after load without an HTMX
    // request behind it.
    document.addEventListener("DOMContentLoaded", () => setTimeout(scrollToHash, 400));
}
