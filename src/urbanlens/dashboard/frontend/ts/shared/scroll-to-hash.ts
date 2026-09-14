/**
 * Bring the URL's target anchor into view once it exists.
 */

/**
 * Find the element a fragment refers to.
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

// The hash last successfully scrolled to.
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
