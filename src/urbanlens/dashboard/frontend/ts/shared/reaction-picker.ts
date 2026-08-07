/**
 * Emoji reaction picker for comments, and its most-recently-used list.
 *
 * The picker's full emoji grid is rendered server-side into each comment's
 * ``.reaction-picker-popup``. This adds a "Recent" row on top, built by cloning the
 * matching buttons out of that grid rather than constructing new ones - the clones
 * carry the server's ``hx-post`` attributes with them, so a recent emoji posts the
 * reaction through exactly the same endpoint as the original. That is why each clone
 * is handed to ``htmx.process``: attributes copied via ``cloneNode`` are inert until
 * htmx is told about them.
 *
 * Ported out of ``base.html``'s inline script unchanged.
 */

const RECENT_KEY = "urbanlens.recentReactionEmojis";
const RECENT_LIMIT = 5;

export function getRecentReactionEmojis(): string[] {
    try {
        const parsed: unknown = JSON.parse(window.localStorage.getItem(RECENT_KEY) || "[]");
        return Array.isArray(parsed) ? parsed.filter((emoji): emoji is string => typeof emoji === "string" && emoji.length > 0).slice(0, RECENT_LIMIT) : [];
    } catch {
        return [];
    }
}

export function saveRecentReactionEmoji(emoji: string): void {
    if (!emoji) return;
    const recent = [emoji, ...getRecentReactionEmojis().filter((item) => item !== emoji)];
    try {
        window.localStorage.setItem(RECENT_KEY, JSON.stringify(recent.slice(0, RECENT_LIMIT)));
    } catch {
        /* Browsers can disable localStorage; reactions should still work normally. */
    }
}

export function refreshRecentReactionEmojis(popup: HTMLElement): void {
    const recent = getRecentReactionEmojis();
    let section = popup.querySelector<HTMLElement>(".reaction-picker-recent");
    if (!section) {
        section = document.createElement("div");
        section.className = "reaction-picker-recent";
        section.innerHTML = '<div class="reaction-picker-label">Recent</div><div class="reaction-picker-recent-list"></div><div class="reaction-picker-divider" aria-hidden="true"></div>';
        popup.insertBefore(section, popup.firstChild);
    }

    const list = section.querySelector<HTMLElement>(".reaction-picker-recent-list");
    if (!list) return;
    list.innerHTML = "";
    for (const emoji of recent) {
        // Excluding the recent clones themselves, or a re-render would clone a clone.
        const source = Array.from(popup.querySelectorAll<HTMLElement>(".reaction-picker-emoji:not(.reaction-picker-emoji--recent)")).find(
            (btn) => (btn.dataset.emoji || btn.textContent?.trim()) === emoji,
        );
        if (!source) continue;
        const clone = source.cloneNode(true) as HTMLElement;
        clone.classList.add("reaction-picker-emoji--recent");
        clone.setAttribute("title", `Use recent reaction ${emoji}`);
        list.appendChild(clone);
        if (window.htmx) window.htmx.process(clone);
    }
    section.hidden = !list.children.length;
}

export function toggleReactionPicker(btn: HTMLElement): void {
    const popup = btn.parentElement?.querySelector<HTMLElement>(".reaction-picker-popup");
    if (!popup) return;
    // Only one picker open at a time - they are absolutely positioned and would overlap.
    document.querySelectorAll<HTMLElement>(".reaction-picker-popup:not([hidden])").forEach((p) => {
        if (p !== popup) p.hidden = true;
    });
    if (popup.hidden) refreshRecentReactionEmojis(popup);
    popup.hidden = !popup.hidden;
}

/** Record the pick, and close the picker when the click landed outside one. */
function onDocumentClick(event: MouseEvent): void {
    const target = event.target as HTMLElement | null;
    if (!target?.closest) return;

    const emojiButton = target.closest<HTMLElement>(".reaction-picker-emoji");
    if (emojiButton) saveRecentReactionEmoji(emojiButton.dataset.emoji || (emojiButton.textContent?.trim() ?? ""));

    if (!target.closest(".reaction-picker")) {
        document.querySelectorAll<HTMLElement>(".reaction-picker-popup:not([hidden])").forEach((p) => {
            p.hidden = true;
        });
    }
}

declare global {
    interface Window {
        toggleReactionPicker?: typeof toggleReactionPicker;
    }
}

export function installGlobalReactionPicker(): void {
    // Called from inline onclick= in the comment partials.
    window.toggleReactionPicker = toggleReactionPicker;
    document.addEventListener("click", onDocumentClick);
}
