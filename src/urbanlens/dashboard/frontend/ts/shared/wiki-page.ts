/**
 * A community wiki page (``pages/location/wiki.html``): the suggest-edits form, keeping every copy of the name
 * in step, the recently-viewed list, and the notice that pins itself once the header scrolls away.
 */

import { toast } from "./dialogs";
import { sendJson } from "./fetch-json";

export interface RecentWiki {
    slug: string;
    title: string;
    subtitle: string;
    url: string;
}

const RECENT_LIMIT = 10;

interface EditResponse {
    ok?: boolean;
    message?: string;
    revision?: number | string;
    about_html?: string;
}

function isRecentWiki(value: unknown): value is RecentWiki {
    return typeof value === "object" && value !== null && "slug" in value && typeof value.slug === "string";
}

/** Put this wiki first in the viewer's recently-viewed list (the home page's strip reads it). */
export function rememberRecentWiki(key: string, entry: RecentWiki): void {
    try {
        let stored: unknown = [];
        try {
            stored = JSON.parse(localStorage.getItem(key) ?? "[]");
        } catch {
            stored = [];
        }
        const others = Array.isArray(stored) ? stored.filter(isRecentWiki).filter((item) => item.slug !== entry.slug) : [];
        localStorage.setItem(key, JSON.stringify([entry, ...others].slice(0, RECENT_LIMIT)));
    } catch {
        // Storage unavailable: nothing to remember into.
    }
}

/** The body event pages/location/wiki.html's history panel reloads on. */
export const WIKI_HISTORY_CHANGED = "wikiHistoryChanged";

function refreshHistory(): void {
    // A panel still showing its placeholder has not been opened, and loads current history when it is.
    const history = document.getElementById("wiki-tab-content");
    if (history && !history.querySelector(".wiki-loading")) document.body.dispatchEvent(new CustomEvent(WIKI_HISTORY_CHANGED));
}

/** Swap in the server's About card, which carries the description, dates and security chips. */
function replaceAbout(html: string): void {
    const existing = document.getElementById("wiki-about-card");
    if (existing) existing.outerHTML = html;
    else if (html) document.querySelector(".wiki-body")?.insertAdjacentHTML("afterbegin", html);
}

/**
 * Save the suggest-edits form as JSON.
 *
 * Every field is sent, not only the changed ones; the server diffs against what this viewer was shown (see
 * "forms submit and save every field" in docs/PROBLEMS.md).
 */
export function installWikiEditForm(form: HTMLElement | null, editUrl: string): void {
    if (!(form instanceof HTMLFormElement)) return;
    form.addEventListener("submit", (event) => {
        event.preventDefault();
        const data: Record<string, string> = {};
        new FormData(form).forEach((value, key) => {
            if (key !== "csrfmiddlewaretoken" && typeof value === "string") data[key] = value;
        });
        void sendJson<EditResponse>(editUrl, "POST", data, { reportsItsOwnErrors: true }).then(
            (resp) => {
                const revision = form.elements.namedItem("base_revision_id");
                if (resp?.revision !== undefined && revision instanceof HTMLInputElement) revision.value = String(resp.revision);
                toast.success(resp?.message ?? "Changes saved.");
                form.closest("dialog")?.close();
                if (resp?.about_html !== undefined) replaceAbout(resp.about_html);
                // installWikiRename's handler refreshes the history too.
                if (data.name) document.body.dispatchEvent(new CustomEvent("wikiRenamed", { detail: { name: data.name } }));
                else refreshHistory();
            },
            (err: unknown) => toast.error(err instanceof Error && err.message ? err.message : "Failed to save changes."),
        );
    });
}

/** ``wikiRenamed`` (this form, or the aliases panel's "Use this name") updates every copy of the name. */
export function installWikiRename(): void {
    document.body.addEventListener("wikiRenamed", (event) => {
        const detail: unknown = event instanceof CustomEvent ? event.detail : null;
        const name = typeof detail === "object" && detail !== null && "name" in detail && typeof detail.name === "string" ? detail.name : "";
        if (!name) return;
        document.querySelectorAll(".wiki-title").forEach((el) => {
            el.textContent = name;
        });
        const field = document.getElementById("wiki-field-name");
        if (field) field.textContent = name;
        const input = document.getElementById("wiki-name");
        if (input instanceof HTMLInputElement) input.value = name;
        refreshHistory();
    });
}

/** Pin the community-wiki notice to the corner once the site header has scrolled away. */
export function installWikiNoticePin(): void {
    const notice = document.querySelector(".wiki-notice");
    const header = document.querySelector(".app-nav");
    if (!notice || !header) return;
    let ticking = false;
    const update = (): void => {
        ticking = false;
        notice.classList.toggle("wiki-notice--pinned", header.getBoundingClientRect().bottom <= 0);
    };
    window.addEventListener(
        "scroll",
        () => {
            if (ticking) return;
            ticking = true;
            requestAnimationFrame(update);
        },
        { passive: true },
    );
    update();
}
