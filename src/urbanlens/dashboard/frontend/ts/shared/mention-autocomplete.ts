/**
 * ``@`` autocomplete for comment boxes.
 *
 * Two kinds of mention share the trigger, chosen by what follows the ``@``:
 * a numeric fragment inside a trip comment offers that trip's numbered activities
 * and inserts ``@act:N``; anything else searches locations and inserts the markup
 * link form ``@[Name](loc:uuid)``.
 *
 * Delegated from ``document`` rather than bound per textarea: comment forms arrive
 * via HTMX swaps throughout the page's life, and a per-element binding would miss
 * every one that appeared after load.
 *
 * Ported out of ``base.html``'s inline script unchanged.
 */

export interface MentionItem {
    name: string;
    /** Present for activity mentions only. */
    actIndex?: number;
    /** Present for location mentions only. */
    uuid?: string;
}

const DEBOUNCE_MS = 200;

let timer: ReturnType<typeof setTimeout> | null = null;
let inFlight: AbortController | null = null;

/** The ``@`` fragment the caret currently sits in, or null if it is not in one. */
function currentQuery(ta: HTMLTextAreaElement): { start: number; query: string } | null {
    const before = ta.value.substring(0, ta.selectionStart);
    const atIdx = before.lastIndexOf("@");
    if (atIdx === -1) return null;
    const frag = before.substring(atIdx + 1);
    // Whitespace ends a mention, so the caret is past this "@" rather than inside it.
    if (/\s/.test(frag)) return null;
    return { start: atIdx, query: frag };
}

function dropdownFor(ta: HTMLTextAreaElement): HTMLElement | null {
    return ta.closest(".comment-input-wrap")?.querySelector<HTMLElement>(".mention-dropdown") ?? null;
}

function hideDropdown(dd: HTMLElement | null): void {
    if (!dd) return;
    dd.hidden = true;
    dd.innerHTML = "";
}

function showDropdown(ta: HTMLTextAreaElement, items: MentionItem[], onSelect: (item: MentionItem) => void): void {
    const dd = dropdownFor(ta);
    if (!dd) return;
    dd.innerHTML = "";
    if (!items.length) {
        dd.hidden = true;
        return;
    }
    for (const item of items) {
        const div = document.createElement("div");
        div.className = "mention-option";
        div.innerHTML = `<span class="mention-option__name">${item.name.replace(/&/g, "&amp;").replace(/</g, "&lt;")}</span>`;
        // mousedown, not click: the textarea would lose focus on blur before a click
        // ever landed, and preventDefault here keeps the caret where it was.
        div.addEventListener("mousedown", (e) => {
            e.preventDefault();
            onSelect(item);
            hideDropdown(dd);
        });
        dd.appendChild(div);
    }
    dd.hidden = false;
}

function insert(ta: HTMLTextAreaElement, start: number, text: string): void {
    const pos = ta.selectionStart;
    const after = ta.value.substring(pos);
    // A trailing space separates the mention from what follows, but only when there
    // is not already one there - inserting mid-sentence used to leave a double space.
    const separator = /^\s/.test(after) ? "" : " ";
    ta.value = `${ta.value.substring(0, start)}${text}${separator}${after}`;
    const cursor = start + text.length + 1;
    ta.setSelectionRange(cursor, cursor);
    ta.focus();
}

/**
 * Whether a response is still wanted by the time it lands.
 *
 * Debouncing alone does not make lookups safe: two can still be outstanding at
 * once, and the network decides which finishes last. Without this the dropdown
 * could offer results for a fragment the box no longer contains, and picking one
 * would insert a location the user never searched for.
 */
function stillCurrent(ta: HTMLTextAreaElement, query: string): boolean {
    return currentQuery(ta)?.query === query;
}

function fetchSuggestions(ta: HTMLTextAreaElement): void {
    // Whatever was asked for before this is now known to be stale.
    inFlight?.abort();
    inFlight = null;

    const q = currentQuery(ta);
    if (!q || q.query.length < 1) {
        hideDropdown(dropdownFor(ta));
        return;
    }

    const context = ta.dataset.contextType || "";
    const tripSlug = ta.dataset.tripSlug || "";
    const controller = new AbortController();
    inFlight = controller;
    const request = { headers: { "X-Requested-With": "XMLHttpRequest" }, signal: controller.signal };

    if (/^\d+$/.test(q.query) && context === "trip" && tripSlug) {
        fetch(`/dashboard/trips/${encodeURIComponent(tripSlug)}/map-data/`, request)
            .then((r) => r.json())
            .then((data: { points?: { index: number; label: string }[] }) => {
                if (!stillCurrent(ta, q.query)) return;
                const points = (data.points || []).filter((p) => String(p.index).startsWith(q.query));
                showDropdown(
                    ta,
                    points.map((p) => ({ name: `#${p.index} - ${p.label}`, actIndex: p.index })),
                    (item) => insert(ta, q.start, `@act:${item.actIndex}`),
                );
            })
            // Swallows genuine failures and our own aborts alike - both mean "no
            // suggestions", and neither is worth interrupting someone mid-sentence.
            .catch(() => {});
        return;
    }

    fetch(`/dashboard/comments/locations/?q=${encodeURIComponent(q.query)}`, request)
        .then((r) => r.json())
        .then((results: MentionItem[]) => {
            if (!stillCurrent(ta, q.query)) return;
            showDropdown(ta, results, (item) => insert(ta, q.start, `@[${item.name}](loc:${item.uuid})`));
        })
        .catch(() => {});
}

function isMentionInput(target: EventTarget | null): target is HTMLTextAreaElement {
    return target instanceof HTMLElement && target.classList.contains("mention-input");
}

function onInput(event: Event): void {
    if (!isMentionInput(event.target)) return;
    const ta = event.target;
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => fetchSuggestions(ta), DEBOUNCE_MS);
}

function onKeydown(event: KeyboardEvent): void {
    if (!isMentionInput(event.target)) return;
    const dd = dropdownFor(event.target);
    if (!dd || dd.hidden) return;

    const options = Array.from(dd.querySelectorAll<HTMLElement>(".mention-option"));
    const current = dd.querySelector<HTMLElement>(".mention-option.is-selected");
    const idx = current ? options.indexOf(current) : -1;

    if (event.key === "ArrowDown") {
        event.preventDefault();
        current?.classList.remove("is-selected");
        options[Math.min(idx + 1, options.length - 1)]?.classList.add("is-selected");
    } else if (event.key === "ArrowUp") {
        event.preventDefault();
        current?.classList.remove("is-selected");
        options[Math.max(idx - 1, 0)]?.classList.add("is-selected");
    } else if ((event.key === "Enter" || event.key === "Tab") && current) {
        event.preventDefault();
        // Reuses the option's own mousedown handler so keyboard and mouse selection
        // cannot drift apart.
        current.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    } else if (event.key === "Escape") {
        hideDropdown(dd);
    }
}

function onClick(event: MouseEvent): void {
    if (isMentionInput(event.target)) return;
    document.querySelectorAll<HTMLElement>(".mention-dropdown:not([hidden])").forEach(hideDropdown);
}

/** Reset module state. Test-only: cancels a pending debounce and lookup between cases. */
export function resetMentionAutocompleteForTests(): void {
    if (timer) clearTimeout(timer);
    timer = null;
    inFlight?.abort();
    inFlight = null;
}

export function installGlobalMentionAutocomplete(): void {
    document.addEventListener("input", onInput);
    document.addEventListener("keydown", onKeydown);
    document.addEventListener("click", onClick);
}
