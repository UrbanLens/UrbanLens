/*
 * WYSIWYG canvas for the pin/wiki article editor - a rich, click-to-format
 * editing surface (TipTap/ProseMirror) that mirrors into the existing
 * Markdown <textarea> on every change, so all of article-editor.js's
 * existing machinery (dirty tracking, char count, debounced live preview,
 * beforeunload guard, HTMX save/cancel) keeps working completely unchanged -
 * this module only owns the WYSIWYG canvas and toolbar routing.
 *
 * Markup contract (see partials/articles/_article_editor.html):
 *   [data-article-editor]        editor root (also carries data-editor-mode,
 *                                 set to "wysiwyg" once mounted, or "source"
 *                                 while the raw textarea is shown)
 *   [data-article-canvas]        empty container TipTap mounts into
 *   [data-article-textarea]      the Markdown textarea (article-editor.js's
 *                                 source of truth; this module keeps it synced)
 *   [data-article-mode-toggle]   Source/Visual switch button
 *
 * Progressive enhancement: the textarea is server-rendered and fully
 * functional on its own (article-editor.js's raw-Markdown toolbar). If this
 * script fails to load, the editor root's data-editor-mode is simply never
 * set to "wysiwyg", so the raw textarea + old toolbar keep working exactly
 * as before - nothing here is load-bearing for basic editing.
 */

import { Editor } from "@tiptap/core";
import { Image } from "@tiptap/extension-image";
import { Placeholder } from "@tiptap/extension-placeholder";
import { TableKit } from "@tiptap/extension-table";
import StarterKit from "@tiptap/starter-kit";
import { Markdown } from "tiptap-markdown";
import { nextReferenceNumber, referenceDefinitionStub } from "../shared/article-footnotes";

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

const editors = new WeakMap<HTMLElement, Editor>();

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

type ToolbarAction = (editor: Editor, root: HTMLElement) => void;

const TOOLBAR_ACTIONS: Record<string, ToolbarAction> = {
    bold: (editor) => editor.chain().focus().toggleBold().run(),
    italic: (editor) => editor.chain().focus().toggleItalic().run(),
    strike: (editor) => editor.chain().focus().toggleStrike().run(),
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
    image: (editor) => {
        const url = window.prompt("Image URL", "https://");
        if (!url) return;
        const alt = window.prompt("Alt text (for accessibility)", "") ?? "";
        editor.chain().focus().setImage({ src: url, alt }).run();
    },
    reference: (editor, root) => insertReference(root, editor),
};

function mountEditor(root: HTMLElement): void {
    if (editors.has(root)) return;
    const textarea = textareaOf(root);
    const canvas = canvasOf(root);
    if (!textarea || !canvas) return;

    const editor = new Editor({
        element: canvas,
        extensions: [
            StarterKit.configure({
                link: { openOnClick: false, autolink: true, defaultProtocol: "https" },
                heading: { levels: [2, 3, 4, 5, 6] },
            }),
            Image,
            TableKit.configure({ table: { resizable: false } }),
            Placeholder.configure({ placeholder: "Start writing…" }),
            Markdown.configure({ html: false, linkify: true, transformPastedText: true }),
        ],
        content: textarea.value,
        editorProps: {
            // Shares the read-mode/preview typography (_article.scss) - only
            // the editor's own chrome is styled separately, via
            // .article-editor-canvas .ProseMirror.
            attributes: { class: "article-body" },
        },
        onUpdate: () => syncTextareaFromEditor(root, editor),
    });

    editors.set(root, editor);
    setMode(root, "wysiwyg");
}

function destroyEditor(root: HTMLElement): void {
    editors.get(root)?.destroy();
    editors.delete(root);
}

function initAll(container: ParentNode): void {
    container.querySelectorAll<HTMLElement>("[data-article-editor]").forEach(mountEditor);
}

// Capture phase, so this always runs before article-editor.js's own
// bubble-phase delegated click handler - when in WYSIWYG mode we handle the
// toolbar click ourselves and stop it from also mutating the (hidden)
// textarea directly via the old raw-Markdown action map.
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
        }
    },
    true,
);

document.body.addEventListener("htmx:afterSwap", (event) => {
    const target = (event as CustomEvent<{ target?: unknown }>).detail?.target;
    initAll(target instanceof Element ? target : document);
});

document.body.addEventListener("htmx:beforeSwap", (event) => {
    const target = (event as CustomEvent<{ target?: unknown }>).detail?.target;
    if (!(target instanceof Element)) return;
    target.querySelectorAll<HTMLElement>("[data-article-editor]").forEach(destroyEditor);
});

initAll(document);
