/* * WYSIWYG canvas for the pin/wiki article editor. */

import { Editor, Extension, type Range } from "@tiptap/core";
import { BubbleMenu } from "@tiptap/extension-bubble-menu";
import { FloatingMenu } from "@tiptap/extension-floating-menu";
import { Image } from "@tiptap/extension-image";
import { Placeholder } from "@tiptap/extension-placeholder";
import { TableKit } from "@tiptap/extension-table";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";
import StarterKit from "@tiptap/starter-kit";
import Suggestion, { type SuggestionProps } from "@tiptap/suggestion";
import { Markdown } from "tiptap-markdown";
import { nextReferenceNumber, referenceDefinitionStub } from "../shared/article-footnotes";
import { anchorSlug } from "../shared/article-toc-anchors";
import { getCsrfToken } from "../shared/csrf";
import { confirmAction } from "../shared/dialogs";

interface MarkdownStorage {
    getMarkdown(): string;
}

// tiptap-markdown ships its own MarkdownStorage type but doesn't augment @tiptap/core's Storage interface itself.
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

// The Source/Clear buttons live in the pin-detail actions menu, outside the editor's own DOM subtree, so ancestry lookup finds nothing.
function editorRootForControl(el: Element | null): HTMLElement | null {
    return editorRoot(el) ?? document.querySelector<HTMLElement>("[data-article-editor]");
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
    // Not root.querySelector: the toggle button now lives in the pin-detail
    // actions menu, outside root's own subtree (see editorRootForControl).
    const toggle = document.querySelector<HTMLElement>("[data-article-mode-toggle]");
    if (toggle) {
        toggle.classList.toggle("is-active", mode === "source");
        toggle.title = mode === "source" ? "Switch to the visual editor" : "View/edit Markdown source";
    }
}

/**
 * Blanks the article (e.g. to discard a Wikipedia-seeded starting point and write from scratch) after confirming.
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
        // emitUpdate: true fires onUpdate -> syncTextareaFromEditor, which mirrors the empty content into the textarea and marks the editor.
        editor.commands.clearContent(true);
        return;
    }

    const textarea = textareaOf(root);
    if (!textarea) return;
    textarea.value = "";
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

/**
 * Footnotes have no WYSIWYG representation (TipTap has no footnote node).
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
 * Upload a picked file to the article's image endpoint and insert it into the document at the current cursor once stored.
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

// Shared command implementations - reused by the (hidden-in-WYSIWYG-mode) legacy fixed toolbar, the selection bubble menu, and the "/".
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
 * Holds the Editor instance once constructed.
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
 * Custom Extension wrapping @tiptap/suggestion to implement Notion-style "/" block insertion.
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

/**
 * Gives every heading in the canvas the same `id` the server assigned it in `article.toc`.
 */
const HeadingAnchors = Extension.create({
    name: "headingAnchors",

    addProseMirrorPlugins() {
        return [
            new Plugin({
                key: new PluginKey("headingAnchors"),
                props: {
                    decorations(state) {
                        const used = new Set<string>();
                        const decorations: Decoration[] = [];
                        state.doc.descendants((node, pos) => {
                            if (node.type.name !== "heading") return;
                            const id = anchorSlug(node.textContent.trim() || "section", used);
                            decorations.push(Decoration.node(pos, pos + node.nodeSize, { id }));
                        });
                        return DecorationSet.create(state.doc, decorations);
                    },
                },
            }),
        ];
    },
});

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
            HeadingAnchors,
            SlashCommand.configure({ root }),
            BubbleMenu.configure({ element: bubbleMenu.element }),
            FloatingMenu.configure({ element: floatingPlus }),
        ],
        content: textarea.value,
        editorProps: {
            // Shares the read-mode/preview typography (_article.scss).
            attributes: { class: "article-body" },
            // Link's own openOnClick is off (a plain click always positions the cursor for editing, never navigates away mid-edit).
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

    // Chromium/WebKit force an I-beam cursor - not just render it, but report it back via getComputedStyle.
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

// container may itself be the swapped-in editor root, and querySelectorAll only matches descendants - never the container itself.
function allMatching(container: ParentNode): HTMLElement[] {
    const matches = Array.from(container.querySelectorAll<HTMLElement>(EDITOR_SELECTOR));
    if (container instanceof HTMLElement && container.matches(EDITOR_SELECTOR)) matches.push(container);
    return matches;
}

function initAll(container: ParentNode): void {
    allMatching(container).forEach(mountEditor);
}

// Capture phase, so this always runs before article-editor.js's own bubble-phase delegated click handler.
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
            const root = editorRootForControl(modeToggle);
            if (!root) return;
            event.preventDefault();
            // A no-op unless the button lives outside the Article tab's own panel (the actions-menu case).
            window.ulActivatePageTab?.("article");
            setMode(root, root.dataset.editorMode === "source" ? "wysiwyg" : "source");
            return;
        }
        const clearButton = target?.closest<HTMLElement>("[data-article-clear]");
        if (clearButton) {
            const root = editorRootForControl(clearButton);
            if (!root) return;
            event.preventDefault();
            window.ulActivatePageTab?.("article");
            void handleClearClick(root);
        }
    },
    true,
);

// htmx:afterSwap's event.detail.target is NOT reliable for scoping here.
document.body.addEventListener("htmx:afterSwap", () => {
    for (const root of editors.keys()) {
        if (!root.isConnected) destroyEditor(root);
    }
    initAll(document);
});

initAll(document);
