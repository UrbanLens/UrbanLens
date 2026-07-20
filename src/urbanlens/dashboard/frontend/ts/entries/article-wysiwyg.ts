/*
 * WYSIWYG canvas for the pin/wiki article editor - a rich, click-to-format
 * editing surface (TipTap/ProseMirror) that mirrors into the existing
 * Markdown <textarea> on every change, so all of article-editor.js's
 * existing machinery (dirty tracking, char count, debounced live preview,
 * beforeunload guard, HTMX save/cancel) keeps working completely unchanged -
 * this module only owns the WYSIWYG canvas and toolbar routing.
 *
 * UX model (Notion-style, not a Markdown editor with a preview bolted on):
 *   - No fixed row of format buttons. Formatting a selection shows a
 *     floating bubble menu right above it (bold/italic/link/headings/...).
 *   - Inserting a block (heading, list, table, image, ...) is done by
 *     typing "/" at the start of a line, which opens a filterable slash
 *     command menu - or by clicking the "+" that appears on an empty line.
 *   - The old fixed toolbar, the live-preview pane, and the Markdown cheat
 *     sheet still exist for Source mode (raw Markdown editing is still
 *     supported and always available via the mode toggle), but are hidden
 *     while the WYSIWYG canvas is active - see the
 *     `[data-editor-mode="wysiwyg"]` rules in _article.scss.
 *
 * Markup contract (see partials/articles/_article_editor.html):
 *   [data-article-editor]        editor root (also carries data-editor-mode,
 *                                 set to "wysiwyg" once mounted, or "source"
 *                                 while the raw textarea is shown)
 *   [data-article-canvas]        empty container TipTap mounts into
 *   [data-article-textarea]      the Markdown textarea (article-editor.js's
 *                                 source of truth; this module keeps it synced)
 *   [data-article-mode-toggle]   Source/Visual switch button
 *   [data-article-clear]         Clears the article's content (after confirming)
 *
 * Progressive enhancement: the textarea is server-rendered and fully
 * functional on its own (article-editor.js's raw-Markdown toolbar). If this
 * script fails to load, the editor root's data-editor-mode is simply never
 * set to "wysiwyg", so the raw textarea + old toolbar keep working exactly
 * as before - nothing here is load-bearing for basic editing.
 */

import { Editor, Extension, type Range } from "@tiptap/core";
import { BubbleMenu } from "@tiptap/extension-bubble-menu";
import { FloatingMenu } from "@tiptap/extension-floating-menu";
import { Image } from "@tiptap/extension-image";
import { Placeholder } from "@tiptap/extension-placeholder";
import { TableKit } from "@tiptap/extension-table";
import StarterKit from "@tiptap/starter-kit";
import Suggestion, { type SuggestionProps } from "@tiptap/suggestion";
import { Markdown } from "tiptap-markdown";
import { nextReferenceNumber, referenceDefinitionStub } from "../shared/article-footnotes";
import { getCsrfToken } from "../shared/csrf";
import { confirmAction } from "../shared/dialogs";

interface MarkdownStorage {
    getMarkdown(): string;
}

// tiptap-markdown ships its own MarkdownStorage type (see its index.d.ts)
// but doesn't augment @tiptap/core's Storage interface itself - do that here
// so `editor.storage.markdown` type-checks instead of needing a cast at
// every call site.
declare module "@tiptap/core" {
    interface Storage {
        markdown: MarkdownStorage;
    }
}

type EditorMode = "wysiwyg" | "source";

// A plain Map (not WeakMap) so mounted roots can be iterated on cleanup - see
// the htmx:afterSwap handler below.
const editors = new Map<HTMLElement, Editor>();

function editorRoot(el: Element | null): HTMLElement | null {
    return el?.closest<HTMLElement>("[data-article-editor]") ?? null;
}

function textareaOf(root: HTMLElement): HTMLTextAreaElement | null {
    return root.querySelector<HTMLTextAreaElement>("[data-article-textarea]");
}

function canvasOf(root: HTMLElement): HTMLElement | null {
    return root.querySelector<HTMLElement>("[data-article-canvas]");
}

function markdownOf(editor: Editor): string {
    return editor.storage.markdown.getMarkdown();
}

function syncTextareaFromEditor(root: HTMLElement, editor: Editor): void {
    const textarea = textareaOf(root);
    if (!textarea) return;
    const markdown = markdownOf(editor);
    if (textarea.value === markdown) return;
    textarea.value = markdown;
    // Re-fires article-editor.js's own input listener: dirty flag, char
    // count, and the debounced live-preview fetch all pick this up for free.
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

function setMode(root: HTMLElement, mode: EditorMode): void {
    const textarea = textareaOf(root);
    const canvas = canvasOf(root);
    const editor = editors.get(root);
    if (!textarea || !canvas || !editor) return;

    if (mode === "source") {
        canvas.hidden = true;
        textarea.hidden = false;
        textarea.focus();
    } else {
        // The textarea may have been hand-edited while in source mode -
        // re-parse its current Markdown back into the WYSIWYG document.
        editor.commands.setContent(textarea.value);
        textarea.hidden = true;
        canvas.hidden = false;
        editor.commands.focus();
    }

    root.dataset.editorMode = mode;
    const toggle = root.querySelector<HTMLElement>("[data-article-mode-toggle]");
    if (toggle) {
        toggle.classList.toggle("is-active", mode === "source");
        toggle.title = mode === "source" ? "Switch to the visual editor" : "View/edit Markdown source";
    }
}

/**
 * Blanks the article (e.g. to discard a Wikipedia-seeded starting point and
 * write from scratch) after confirming - this only clears the in-progress
 * edit, it doesn't save; Cancel still discards the whole thing (including the
 * clear) if the user changes their mind, same safety net as any other edit.
 */
async function handleClearClick(root: HTMLElement): Promise<void> {
    const confirmed = await confirmAction({
        title: "Clear this article?",
        message: "This removes all of the article's current content. Nothing is saved until you click Save, so you can still Cancel afterward to discard the change.",
        confirmLabel: "Clear",
        cancelLabel: "Keep writing",
    });
    if (!confirmed) return;

    const editor = editors.get(root);
    if (editor && root.dataset.editorMode === "wysiwyg") {
        // emitUpdate: true fires onUpdate -> syncTextareaFromEditor, which
        // mirrors the empty content into the textarea and marks the editor
        // dirty - the same path every other WYSIWYG edit already goes through.
        editor.commands.clearContent(true);
        return;
    }

    const textarea = textareaOf(root);
    if (!textarea) return;
    textarea.value = "";
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

/**
 * Footnotes have no WYSIWYG representation (TipTap has no footnote node) -
 * insert the reference marker in the doc as plain text, then switch to
 * Source mode so the user can see and fill in the definition at the bottom
 * of the article, mirroring article-editor.js's own insertReference() for
 * the raw textarea.
 */
function insertReference(root: HTMLElement, editor: Editor): void {
    const n = nextReferenceNumber(markdownOf(editor));
    editor.chain().focus().insertContent(`[^${n}]`).run();
    setMode(root, "source");
    const textarea = textareaOf(root);
    if (!textarea) return;
    textarea.value += referenceDefinitionStub(n, textarea.value);
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    if (window.toastr) window.toastr.info(`Reference [${n}] added - fill in the source at the bottom of the article.`);
}

interface UploadResponse {
    url?: string;
    error?: string;
}

/**
 * Upload a picked file to the article's image endpoint (see
 * ArticleImageUploadView/`data-image-upload-url`) and insert it into the
 * document at the current cursor once stored - the same size/content-type/
 * malware-scan/quota checks as every other gallery upload run server-side
 * before this ever resolves.
 */
async function uploadAndInsertImage(root: HTMLElement, editor: Editor, file: File): Promise<void> {
    const uploadUrl = root.dataset.imageUploadUrl;
    if (!uploadUrl) return;

    const formData = new FormData();
    formData.append("image", file);

    let data: UploadResponse = {};
    let ok = false;
    try {
        const response = await fetch(uploadUrl, { method: "POST", body: formData, headers: { "X-CSRFToken": getCsrfToken() } });
        ok = response.ok;
        data = (await response.json().catch(() => ({}))) as UploadResponse;
    } catch {
        if (window.toastr) window.toastr.error("Image upload failed - check your connection and try again.");
        return;
    }

    if (!ok || !data.url) {
        if (window.toastr) window.toastr.error(data.error || "Image upload failed.");
        return;
    }
    editor.chain().focus().setImage({ src: data.url, alt: file.name }).run();
}

/** Opens the browser's file picker and hands the chosen image off to uploadAndInsertImage. */
function pickAndUploadImage(root: HTMLElement, editor: Editor): void {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*";
    input.addEventListener(
        "change",
        () => {
            const file = input.files?.[0];
            if (file) void uploadAndInsertImage(root, editor, file);
        },
        { once: true },
    );
    input.click();
}

type EditorAction = (editor: Editor, root: HTMLElement) => void;

// Shared command implementations - reused by the (hidden-in-WYSIWYG-mode)
// legacy fixed toolbar, the selection bubble menu, and the "/" slash-command
// menu, so every entry point stays behaviorally identical.
const TOOLBAR_ACTIONS: Record<string, EditorAction> = {
    bold: (editor) => editor.chain().focus().toggleBold().run(),
    italic: (editor) => editor.chain().focus().toggleItalic().run(),
    strike: (editor) => editor.chain().focus().toggleStrike().run(),
    paragraph: (editor) => editor.chain().focus().setParagraph().run(),
    h2: (editor) => editor.chain().focus().toggleHeading({ level: 2 }).run(),
    h3: (editor) => editor.chain().focus().toggleHeading({ level: 3 }).run(),
    ul: (editor) => editor.chain().focus().toggleBulletList().run(),
    ol: (editor) => editor.chain().focus().toggleOrderedList().run(),
    quote: (editor) => editor.chain().focus().toggleBlockquote().run(),
    hr: (editor) => editor.chain().focus().setHorizontalRule().run(),
    code: (editor) => {
        // Matches article-editor.js's own multi-line-selection heuristic for
        // choosing a fenced code block over inline code.
        const { from, to, empty } = editor.state.selection;
        const selected = empty ? "" : editor.state.doc.textBetween(from, to, "\n");
        if (selected.includes("\n")) editor.chain().focus().toggleCodeBlock().run();
        else editor.chain().focus().toggleCode().run();
    },
    codeBlock: (editor) => editor.chain().focus().toggleCodeBlock().run(),
    table: (editor) => editor.chain().focus().insertTable({ rows: 3, cols: 2, withHeaderRow: true }).run(),
    link: (editor) => {
        const previousUrl = editor.getAttributes("link").href as string | undefined;
        const url = window.prompt("Link URL", previousUrl ?? "https://");
        if (url === null) return;
        if (url === "") {
            editor.chain().focus().extendMarkRange("link").unsetLink().run();
            return;
        }
        editor.chain().focus().extendMarkRange("link").setLink({ href: url }).run();
    },
    image: (editor, root) => pickAndUploadImage(root, editor),
    reference: (editor, root) => insertReference(root, editor),
};

// -- Bubble menu (format-on-selection, Notion's core formatting affordance) -

interface BubbleButtonDef {
    action: string;
    icon: string;
    title: string;
    isActive: (editor: Editor) => boolean;
}

const BUBBLE_BUTTONS: BubbleButtonDef[] = [
    { action: "bold", icon: "format_bold", title: "Bold (Ctrl+B)", isActive: (e) => e.isActive("bold") },
    { action: "italic", icon: "format_italic", title: "Italic (Ctrl+I)", isActive: (e) => e.isActive("italic") },
    { action: "strike", icon: "strikethrough_s", title: "Strikethrough", isActive: (e) => e.isActive("strike") },
    { action: "code", icon: "code", title: "Code", isActive: (e) => e.isActive("code") },
    { action: "link", icon: "link", title: "Link (Ctrl+K)", isActive: (e) => e.isActive("link") },
    { action: "h2", icon: "format_h2", title: "Heading", isActive: (e) => e.isActive("heading", { level: 2 }) },
    { action: "h3", icon: "format_h3", title: "Sub-heading", isActive: (e) => e.isActive("heading", { level: 3 }) },
    { action: "quote", icon: "format_quote", title: "Quote", isActive: (e) => e.isActive("blockquote") },
];

/**
 * Holds the Editor instance once constructed. BubbleMenu/FloatingMenu
 * elements must be built and handed to their extensions' `.configure()`
 * before `new Editor(...)` returns, but their button handlers need the
 * editor itself - closing over this mutable box (instead of the editor
 * directly) lets the elements be built first and wired to the real instance
 * right after construction finishes, before anything can click them.
 */
interface EditorBox {
    current: Editor | null;
}

function buildBubbleMenuElement(root: HTMLElement, box: EditorBox): { element: HTMLElement; refresh: () => void } {
    const el = document.createElement("div");
    el.className = "article-bubble-menu";
    el.setAttribute("role", "toolbar");
    el.setAttribute("aria-label", "Format selection");

    const refresh = (): void => {
        const editor = box.current;
        if (!editor) return;
        el.querySelectorAll<HTMLButtonElement>(".article-bubble-btn").forEach((button, index) => {
            const def = BUBBLE_BUTTONS[index];
            if (def) button.classList.toggle("is-active", def.isActive(editor));
        });
    };

    BUBBLE_BUTTONS.forEach((def) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "article-bubble-btn";
        button.title = def.title;
        button.innerHTML = `<i class="material-symbols-outlined">${def.icon}</i>`;
        // Formatting must not steal focus/collapse the selection before the
        // command runs against it.
        button.addEventListener("mousedown", (event) => event.preventDefault());
        button.addEventListener("click", () => {
            const editor = box.current;
            if (!editor) return;
            TOOLBAR_ACTIONS[def.action]?.(editor, root);
            refresh();
        });
        el.appendChild(button);
    });

    return { element: el, refresh };
}

// -- Slash command menu (block insertion, Notion's core "/" affordance) -----

interface SlashItem {
    action: string;
    icon: string;
    label: string;
    keywords: string;
}

const SLASH_ITEMS: SlashItem[] = [
    { action: "paragraph", icon: "notes", label: "Text", keywords: "text paragraph plain" },
    { action: "h2", icon: "format_h2", label: "Heading", keywords: "heading h2 title section" },
    { action: "h3", icon: "format_h3", label: "Sub-heading", keywords: "subheading h3" },
    { action: "ul", icon: "format_list_bulleted", label: "Bulleted list", keywords: "bullet list ul unordered" },
    { action: "ol", icon: "format_list_numbered", label: "Numbered list", keywords: "numbered list ol ordered" },
    { action: "quote", icon: "format_quote", label: "Quote", keywords: "quote blockquote" },
    { action: "codeBlock", icon: "code", label: "Code block", keywords: "code codeblock fenced" },
    { action: "table", icon: "table", label: "Table", keywords: "table grid rows columns" },
    { action: "image", icon: "image", label: "Image", keywords: "image photo picture upload" },
    { action: "hr", icon: "horizontal_rule", label: "Divider", keywords: "divider rule horizontal hr separator" },
    { action: "reference", icon: "superscript", label: "Reference", keywords: "reference footnote citation source" },
];

function filterSlashItems(query: string): SlashItem[] {
    const q = query.trim().toLowerCase();
    if (!q) return SLASH_ITEMS;
    return SLASH_ITEMS.filter((item) => item.keywords.includes(q) || item.label.toLowerCase().includes(q));
}

function renderSlashItems(listEl: HTMLElement, items: SlashItem[], selectedIndex: number, onPick: (item: SlashItem) => void): void {
    listEl.innerHTML = "";
    if (!items.length) {
        const empty = document.createElement("div");
        empty.className = "article-slash-empty";
        empty.textContent = "No matching blocks";
        listEl.appendChild(empty);
        return;
    }
    items.forEach((item, index) => {
        const row = document.createElement("button");
        row.type = "button";
        row.className = "article-slash-item" + (index === selectedIndex ? " is-selected" : "");
        row.setAttribute("role", "option");
        row.innerHTML = `<i class="material-symbols-outlined">${item.icon}</i><span>${item.label}</span>`;
        row.addEventListener("mousedown", (event) => event.preventDefault());
        row.addEventListener("click", () => onPick(item));
        listEl.appendChild(row);
    });
}

/**
 * Custom Extension wrapping @tiptap/suggestion to implement Notion-style
 * "/" block insertion. Triggering on "/" opens a filterable popup (positioned
 * by Suggestion's managed floating-ui mount); picking an item deletes the
 * typed "/query" text and runs the matching block-insert command.
 */
const SlashCommand = Extension.create<{ root: HTMLElement | null }>({
    name: "slashCommand",

    addOptions() {
        return { root: null };
    },

    addProseMirrorPlugins() {
        const editor = this.editor;
        const root = this.options.root;
        if (!root) return [];

        let items: SlashItem[] = [];
        let selectedIndex = 0;
        let listEl: HTMLElement | null = null;
        let unmount: (() => void) | null = null;

        const pick = (range: Range, item: SlashItem): void => {
            editor.chain().focus().deleteRange(range).run();
            TOOLBAR_ACTIONS[item.action]?.(editor, root);
            unmount?.();
        };

        const rerender = (range: Range): void => {
            if (listEl) renderSlashItems(listEl, items, selectedIndex, (item) => pick(range, item));
        };

        return [
            Suggestion({
                editor,
                char: "/",
                startOfLine: false,
                items: ({ query }) => filterSlashItems(query),
                render: () => ({
                    onStart: (props: SuggestionProps<SlashItem>) => {
                        items = props.items;
                        selectedIndex = 0;
                        listEl = document.createElement("div");
                        listEl.className = "article-slash-menu";
                        listEl.setAttribute("role", "listbox");
                        rerender(props.range);
                        unmount = props.mount(listEl);
                    },
                    onUpdate: (props: SuggestionProps<SlashItem>) => {
                        items = props.items;
                        selectedIndex = 0;
                        rerender(props.range);
                    },
                    onKeyDown: (props) => {
                        if (props.event.key === "Escape") {
                            unmount?.();
                            return true;
                        }
                        if (!items.length) return false;
                        if (props.event.key === "ArrowDown") {
                            selectedIndex = (selectedIndex + 1) % items.length;
                            rerender(props.range);
                            return true;
                        }
                        if (props.event.key === "ArrowUp") {
                            selectedIndex = (selectedIndex - 1 + items.length) % items.length;
                            rerender(props.range);
                            return true;
                        }
                        if (props.event.key === "Enter" || props.event.key === "Tab") {
                            const selected = items[selectedIndex];
                            if (selected) pick(props.range, selected);
                            return true;
                        }
                        return false;
                    },
                    onExit: () => {
                        unmount?.();
                        listEl = null;
                    },
                }),
            }),
        ];
    },
});

/**
 * "+" affordance shown on an empty line (Notion's mouse-driven equivalent of
 * typing "/") - inserting the trigger character hands off to SlashCommand's
 * already-wired Suggestion plugin instead of duplicating the popup.
 */
function buildFloatingPlusElement(box: EditorBox): HTMLElement {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "article-floating-plus";
    button.title = "Add a block";
    button.innerHTML = '<i class="material-symbols-outlined">add</i>';
    button.addEventListener("mousedown", (event) => event.preventDefault());
    button.addEventListener("click", () => box.current?.chain().focus().insertContent("/").run());
    return button;
}

function mountEditor(root: HTMLElement): void {
    if (editors.has(root)) return;
    const textarea = textareaOf(root);
    const canvas = canvasOf(root);
    if (!textarea || !canvas) return;

    const editorBox: EditorBox = { current: null };
    const bubbleMenu = buildBubbleMenuElement(root, editorBox);
    const floatingPlus = buildFloatingPlusElement(editorBox);

    const editor = new Editor({
        element: canvas,
        extensions: [
            StarterKit.configure({
                link: { openOnClick: false, autolink: true, defaultProtocol: "https" },
                heading: { levels: [2, 3, 4, 5, 6] },
            }),
            Image,
            TableKit.configure({ table: { resizable: false } }),
            Placeholder.configure({ placeholder: "Start writing, or type “/” to insert a block…" }),
            Markdown.configure({ html: false, linkify: true, transformPastedText: true }),
            SlashCommand.configure({ root }),
            BubbleMenu.configure({ element: bubbleMenu.element }),
            FloatingMenu.configure({ element: floatingPlus }),
        ],
        content: textarea.value,
        editorProps: {
            // Shares the read-mode/preview typography (_article.scss) - only
            // the editor's own chrome is styled separately, via
            // .article-editor-canvas .ProseMirror.
            attributes: { class: "article-body" },
            // Link's own openOnClick is off (a plain click always positions
            // the cursor for editing, never navigates away mid-edit) - a
            // Ctrl/Cmd+click is the one exception, the same convention most
            // rich-text editors use for "this is still a real link". Pairs
            // with the hover-driven contenteditable toggle below, which is
            // what actually lets the hovered cursor show as a pointer -
            // Chromium/WebKit force an I-beam cursor for anything inside a
            // contenteditable region regardless of its own CSS `cursor`
            // value, so the pointer only shows once a link is genuinely
            // carved out of the editable region, not just styled to look
            // like it is.
            handleClick: (_view, _pos, event) => {
                if (!(event.metaKey || event.ctrlKey)) return false;
                const link = (event.target as HTMLElement | null)?.closest("a[href]");
                if (!link) return false;
                window.open(link.getAttribute("href") ?? "", "_blank", "noopener,noreferrer");
                return true;
            },
        },
        onUpdate: () => syncTextareaFromEditor(root, editor),
    });

    editorBox.current = editor;
    editor.on("transaction", bubbleMenu.refresh);
    editor.on("selectionUpdate", bubbleMenu.refresh);

    // Chromium/WebKit force an I-beam cursor - not just render it, but report
    // it back via getComputedStyle - for anything inside a contenteditable
    // region, no matter what CSS says (confirmed live: not even
    // `cursor: pointer !important` changes the computed value). Briefly
    // marking a hovered link contenteditable="false" is the one thing that
    // genuinely changes Chromium's editability determination for cursor
    // purposes, so the browser's own normal link-hover cursor takes over.
    // Undone again on mousedown (before the click/selection logic runs) and
    // on mouseleave, so this is purely a hover-only visual cue - actual
    // clicks always see a normal, still-editable link, whether that's
    // ProseMirror's own click-to-position-cursor or the Ctrl/Cmd+click
    // handler above.
    canvas.addEventListener("mouseover", (event) => {
        const link = (event.target as HTMLElement | null)?.closest("a[href]");
        if (link) link.setAttribute("contenteditable", "false");
    });
    canvas.addEventListener("mouseout", (event) => {
        const link = (event.target as HTMLElement | null)?.closest("a[href]");
        if (link) link.removeAttribute("contenteditable");
    });
    canvas.addEventListener("mousedown", (event) => {
        const link = (event.target as HTMLElement | null)?.closest("a[href]");
        if (link) link.removeAttribute("contenteditable");
    });

    editors.set(root, editor);
    setMode(root, "wysiwyg");
}

function destroyEditor(root: HTMLElement): void {
    editors.get(root)?.destroy();
    editors.delete(root);
}

const EDITOR_SELECTOR = "[data-article-editor]";

// container may itself be the swapped-in editor root, and querySelectorAll
// only matches descendants - never the container itself - so that case has
// to be checked separately or the editor never mounts.
function allMatching(container: ParentNode): HTMLElement[] {
    const matches = Array.from(container.querySelectorAll<HTMLElement>(EDITOR_SELECTOR));
    if (container instanceof HTMLElement && container.matches(EDITOR_SELECTOR)) matches.push(container);
    return matches;
}

function initAll(container: ParentNode): void {
    allMatching(container).forEach(mountEditor);
}

// Capture phase, so this always runs before article-editor.js's own
// bubble-phase delegated click handler - when in WYSIWYG mode we handle the
// toolbar click ourselves and stop it from also mutating the (hidden)
// textarea directly via the old raw-Markdown action map. The fixed toolbar
// buttons themselves are hidden while WYSIWYG is active (see
// [data-editor-mode="wysiwyg"] in _article.scss - formatting now happens via
// the bubble/slash menus instead) but this still backs Source mode's toolbar.
document.addEventListener(
    "click",
    (event) => {
        const target = event.target instanceof Element ? event.target : null;
        const toolButton = target?.closest<HTMLElement>("[data-md-action]");
        if (toolButton) {
            const root = editorRoot(toolButton);
            if (!root || root.dataset.editorMode !== "wysiwyg") return;
            const editor = editors.get(root);
            const action = TOOLBAR_ACTIONS[toolButton.dataset.mdAction ?? ""];
            if (editor && action) {
                event.preventDefault();
                event.stopImmediatePropagation();
                action(editor, root);
            }
            return;
        }
        const modeToggle = target?.closest<HTMLElement>("[data-article-mode-toggle]");
        if (modeToggle) {
            const root = editorRoot(modeToggle);
            if (!root) return;
            event.preventDefault();
            setMode(root, root.dataset.editorMode === "source" ? "wysiwyg" : "source");
            return;
        }
        const clearButton = target?.closest<HTMLElement>("[data-article-clear]");
        if (clearButton) {
            const root = editorRoot(clearButton);
            if (!root) return;
            event.preventDefault();
            void handleClearClick(root);
        }
    },
    true,
);

// htmx:afterSwap's event.detail.target is NOT reliable for scoping here: for
// `outerHTML` swaps (which the editor's own save/cancel/mode-toggle all use)
// it's htmx's reference to the *old*, already-detached element being replaced
// - not the new one that took its place - so a mounted editor's canvas would
// never be found by searching within it. Rescanning the whole document is
// what actually works; mountEditor() is idempotent (the `editors` Map guard)
// so this is cheap and safe on every swap anywhere on the page. Cleanup uses
// the same rescan: any previously-tracked root no longer connected to the
// document (because it - or an ancestor - just got swapped out) is disposed.
document.body.addEventListener("htmx:afterSwap", () => {
    for (const root of editors.keys()) {
        if (!root.isConnected) destroyEditor(root);
    }
    initAll(document);
});

initAll(document);
