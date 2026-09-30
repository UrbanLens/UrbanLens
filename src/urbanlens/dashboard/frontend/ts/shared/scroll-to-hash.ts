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

/** Open the collapsed sections hiding the target, and the target itself when it is one; true if any was closed. */
function reveal(target: Element): boolean {
    let opened = false;
    for (let el: Element | null = target; el; el = el.parentElement) {
        if (el instanceof HTMLDetailsElement && !el.open) {
            el.open = true;
            opened = true;
        }
    }
    return opened;
}

function bringIntoView(target: Element): void {
    // An answer can be taller than the viewport, so its question goes to the top rather than the middle.
    target.scrollIntoView({ behavior: "smooth", block: target instanceof HTMLDetailsElement ? "start" : "center" });
}

// The hash last successfully scrolled to.
let scrolledToHash = "";

export function scrollToHash(): void {
    const hash = window.location.hash;
    if (!hash || hash === scrolledToHash) return;
    const target = findTarget(hash);
    if (!target) return; // not rendered yet - a later settle will retry
    reveal(target);
    bringIntoView(target);
    scrolledToHash = hash;
}

/** Reset the "already scrolled here" memory. Test-only: a real page navigation
 * already gets a fresh module instance. */
export function resetScrollToHashForTests(): void {
    scrolledToHash = "";
}

export function installGlobalScrollToHash(): void {
    document.addEventListener("htmx:afterSettle", scrollToHash);
    // An in-page link already jumps natively; it needs help only when its target was collapsed.
    window.addEventListener("hashchange", () => {
        const target = findTarget(window.location.hash);
        if (target && reveal(target)) bringIntoView(target);
    });
    // The delay covers content that renders shortly after load without an HTMX
    // request behind it.
    document.addEventListener("DOMContentLoaded", () => setTimeout(scrollToHash, 400));
}
