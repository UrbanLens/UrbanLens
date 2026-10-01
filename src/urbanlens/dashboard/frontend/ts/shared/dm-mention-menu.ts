/**
 * The Messages composer's "@" menu: typing "@" (or pressing the @ button) lists the share actions under the caret,
 * filtering as the query grows. Picking one removes the typed "@query" and hands the kind to the page.
 */

export type MentionKind = "pin" | "trip" | "map" | "friend";

interface MentionAction {
    kind: MentionKind;
    icon: string;
    label: string;
}

export const MENTION_ACTIONS: readonly MentionAction[] = [
    { kind: "pin", icon: "location_on", label: "Share a pin" },
    { kind: "trip", icon: "luggage", label: "Invite to a trip" },
    { kind: "map", icon: "folder_shared", label: "Attach an existing map" },
    { kind: "friend", icon: "person_add", label: "Recommend a friend" },
];

const MENU_ID = "dm-mention-menu";

export interface MentionSpan {
    /** Where the "@" is. */
    start: number;
    /** The caret, just past the query. */
    end: number;
    query: string;
}

/** The mention being typed at *caret*: an "@" at the start or after whitespace, with no whitespace since. */
export function mentionAt(value: string, caret: number): MentionSpan | null {
    if (caret <= 0) return null;
    const at = value.lastIndexOf("@", caret - 1);
    if (at === -1 || (at > 0 && !/\s/.test(value.charAt(at - 1)))) return null;
    const query = value.slice(at + 1, caret);
    return /\s/.test(query) ? null : { start: at, end: caret, query };
}

export function matchingMentionActions(query: string): MentionAction[] {
    const lower = query.toLowerCase();
    if (!lower) return [...MENTION_ACTIONS];
    return MENTION_ACTIONS.filter((opt) => opt.kind.startsWith(lower) || opt.label.toLowerCase().includes(lower));
}

const MIRROR_PROPS = [
    "boxSizing",
    "width",
    "paddingTop",
    "paddingRight",
    "paddingBottom",
    "paddingLeft",
    "borderTopWidth",
    "borderRightWidth",
    "borderBottomWidth",
    "borderLeftWidth",
    "fontFamily",
    "fontSize",
    "fontWeight",
    "lineHeight",
    "letterSpacing",
    "textTransform",
    "wordSpacing",
] as const;

/** Where character *position* lands inside *textarea*, measured on an off-screen copy: textareas have no API for it. */
function caretCoordinates(textarea: HTMLTextAreaElement, position: number): { top: number; left: number; lineHeight: number } {
    const style = window.getComputedStyle(textarea);
    const mirror = document.createElement("div");
    for (const prop of MIRROR_PROPS) mirror.style[prop] = style[prop];
    Object.assign(mirror.style, { position: "absolute", visibility: "hidden", whiteSpace: "pre-wrap", overflowWrap: "break-word", top: "0", left: "-9999px", height: "auto" });
    mirror.textContent = textarea.value.slice(0, position);
    const marker = document.createElement("span");
    marker.textContent = "​";
    mirror.appendChild(marker);
    document.body.appendChild(mirror);
    const coords = { top: marker.offsetTop, left: marker.offsetLeft, lineHeight: Number.parseInt(style.lineHeight, 10) || 20 };
    mirror.remove();
    return coords;
}

export class MentionMenu {
    private span: MentionSpan | null = null;

    constructor(
        private readonly input: () => HTMLTextAreaElement | null,
        private readonly onPick: (kind: MentionKind) => void,
        /** After the menu edits the composer's text, e.g. to resize it. */
        private readonly afterEdit: (input: HTMLTextAreaElement) => void = () => undefined,
    ) {}

    get isOpen(): boolean {
        return this.span !== null;
    }

    /** Follow the composer's text: open or refilter on a mention in progress, close otherwise. */
    track(): void {
        const input = this.input();
        const span = input ? mentionAt(input.value, input.selectionStart) : null;
        if (span) this.show(span);
        else this.close();
    }

    /** Insert an "@" at the caret (after a space if needed) and open the menu on it. */
    insertTrigger(): void {
        const input = this.input();
        if (!input) return;
        const caret = input.selectionStart;
        const insert = caret > 0 && !/\s/.test(input.value.charAt(caret - 1)) ? " @" : "@";
        input.value = input.value.slice(0, caret) + insert + input.value.slice(caret);
        const end = caret + insert.length;
        input.focus();
        input.selectionStart = input.selectionEnd = end;
        this.afterEdit(input);
        this.show({ start: end - 1, end, query: "" });
    }

    close(): void {
        document.getElementById(MENU_ID)?.remove();
        this.span = null;
    }

    /** Handles the menu's keys while it is open; true when the key was one of them. */
    handleKey(e: KeyboardEvent): boolean {
        if (!this.span) return false;
        if (e.key === "ArrowDown" || e.key === "ArrowUp") this.step(e.key === "ArrowDown" ? 1 : -1);
        else if (e.key === "Escape") this.close();
        else if (e.key === "Enter" && !e.shiftKey) {
            const kind = this.activeKind();
            if (kind) this.pick(kind);
        } else return false;
        e.preventDefault();
        return true;
    }

    pick(kind: MentionKind): void {
        const input = this.input();
        if (input && this.span) {
            const { start, end } = this.span;
            input.value = input.value.slice(0, start) + input.value.slice(end);
            input.focus();
            input.selectionStart = input.selectionEnd = start;
            this.afterEdit(input);
        }
        this.close();
        this.onPick(kind);
    }

    private items(): HTMLElement[] {
        return Array.from(document.querySelectorAll<HTMLElement>(`#${MENU_ID} .dm-mention-menu__item`));
    }

    private activeKind(): MentionKind | null {
        const kind = this.items().find((el) => el.classList.contains("is-active"))?.dataset.kind;
        return MENTION_ACTIONS.find((a) => a.kind === kind)?.kind ?? null;
    }

    private step(delta: number): void {
        const items = this.items();
        if (!items.length) return;
        const current = Math.max(0, items.findIndex((el) => el.classList.contains("is-active")));
        const next = (current + delta + items.length) % items.length;
        items.forEach((el, i) => el.classList.toggle("is-active", i === next));
    }

    private show(span: MentionSpan): void {
        const input = this.input();
        const composer = input?.form;
        if (!input || !composer) return;
        this.span = span;
        let menu = document.getElementById(MENU_ID);
        if (!menu) {
            menu = document.createElement("div");
            menu.id = MENU_ID;
            menu.className = "dm-mention-menu";
            composer.appendChild(menu);
        }
        menu.replaceChildren();
        const matches = matchingMentionActions(span.query);
        if (!matches.length) {
            const empty = document.createElement("div");
            empty.className = "dm-mention-menu__empty";
            empty.textContent = "No matching actions";
            menu.appendChild(empty);
        }
        matches.forEach((opt, index) => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = index === 0 ? "dm-mention-menu__item is-active" : "dm-mention-menu__item";
            btn.dataset.kind = opt.kind;
            const icon = document.createElement("i");
            icon.className = "material-symbols-outlined";
            icon.textContent = opt.icon;
            btn.append(icon, opt.label);
            btn.addEventListener("click", () => this.pick(opt.kind));
            menu.appendChild(btn);
        });
        const coords = caretCoordinates(input, span.start);
        menu.style.left = `${Math.max(0, input.offsetLeft + coords.left)}px`;
        menu.style.top = `${input.offsetTop + coords.top + coords.lineHeight}px`;
    }
}
