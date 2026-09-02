/**
 * The client's own record of recently-dismissed page explainers and
 * onboarding-tour cards (plan §10, batch 4) - a capped sessionStorage ring,
 * never a server-side registry. `_page_explainer_script.html`'s `collapse()`
 * (a plain inline `<script>`, not a bundled module) and `onboarding-tour.ts`'s
 * `dismiss()` both push here; the assistant composer sends the ring's
 * contents with every turn (`_composer.html`) so `recent_dismissals()` /
 * `reopen_explainer()` can answer from what the user's own page just
 * rendered, never anything invented.
 *
 * Exposed on `window.ulDismissalRing` (installed from `entries-classic/core.ts`,
 * which loads before the inline explainer script - see themes/base.html) so
 * that non-module script can call it without an import.
 */

const STORAGE_KEY = "ul_explainer_recent";
const MAX_ENTRIES = 5;
const HEADING_MAX_CHARS = 120;
const BODY_MAX_CHARS = 600;

export interface DismissalEntry {
    id: string;
    kind: "explainer" | "tour";
    heading: string;
    body: string;
    page: string;
    /** The tour's localStorage key prefix - only set for kind "tour". */
    prefix?: string;
}

function readRing(): DismissalEntry[] {
    try {
        const raw = sessionStorage.getItem(STORAGE_KEY);
        const parsed: unknown = raw ? JSON.parse(raw) : [];
        return Array.isArray(parsed) ? (parsed as DismissalEntry[]) : [];
    } catch {
        return [];
    }
}

/**
 * Record a dismissal at the front of the ring, capped at {@link MAX_ENTRIES}.
 *
 * Re-dismissing the same id/kind moves it to the front instead of adding a
 * second entry - the ring is "what's recent", not a full dismissal log.
 */
export function pushDismissal(kind: DismissalEntry["kind"], id: string, heading: string, body: string, prefix?: string): void {
    try {
        const entry: DismissalEntry = {
            id,
            kind,
            heading: heading.slice(0, HEADING_MAX_CHARS),
            body: body.slice(0, BODY_MAX_CHARS),
            page: window.location.pathname,
            ...(prefix ? { prefix } : {}),
        };
        const ring = [entry, ...readRing().filter((existing) => !(existing.id === id && existing.kind === kind))].slice(0, MAX_ENTRIES);
        sessionStorage.setItem(STORAGE_KEY, JSON.stringify(ring));
    } catch {
        /* storage unavailable - the ring is best-effort */
    }
}

export function getRecentDismissals(): DismissalEntry[] {
    return readRing();
}

/** Test-only: empty the ring without touching module state (there is none). */
export function clearDismissalRingForTests(): void {
    try {
        sessionStorage.removeItem(STORAGE_KEY);
    } catch {
        /* ignore */
    }
}

export function installGlobalDismissalRing(): void {
    window.ulDismissalRing = { push: pushDismissal, list: getRecentDismissals };
}

declare global {
    interface Window {
        ulDismissalRing?: {
            push: typeof pushDismissal;
            list: typeof getRecentDismissals;
        };
    }
}
