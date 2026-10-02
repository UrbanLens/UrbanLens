import { describe, expect, test } from "bun:test";
import type { Editor } from "@tiptap/core";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import { comparableRendering, splitArticleSource } from "../shared/article-markdown-syntax";
import { ARTICLE_FIXTURES, HAND_WRITTEN_ARTICLE, SEEDED_ARTICLE, WYSIWYG_ARTICLE } from "../testing/article-fixtures";

interface EditorElement extends HTMLElement {
    editor?: Editor;
}

interface Mounted {
    editor: Editor;
    textarea: HTMLTextAreaElement;
    /** How many input events the textarea has fired since mounting. */
    inputs: () => number;
}

const PANEL = `
    <button type="button" data-article-mode-toggle></button>
    <div data-article-editor>
        <button type="button" data-md-action="reference"></button>
        <div data-article-canvas hidden></div>
        <textarea data-article-textarea></textarea>
    </div>`;

async function mount(source: string): Promise<Mounted> {
    document.body.innerHTML = PANEL;
    const textarea = document.querySelector<HTMLTextAreaElement>("[data-article-textarea]")!;
    textarea.value = source;
    let inputs = 0;
    textarea.addEventListener("input", () => {
        inputs += 1;
    });
    await import("./article-wysiwyg");
    document.body.dispatchEvent(new Event("htmx:afterSwap"));
    const editor = document.querySelector<EditorElement>(".ProseMirror")?.editor;
    if (!editor) throw new Error("the editor did not mount");
    return { editor, textarea, inputs: () => inputs };
}

function positionOf(editor: Editor, needle: string): number {
    let found = -1;
    editor.state.doc.descendants((node, pos) => {
        if (found >= 0) return false;
        const index = node.isText ? (node.text ?? "").indexOf(needle) : -1;
        if (index >= 0) found = pos + index;
        return true;
    });
    if (found < 0) throw new Error(`"${needle}" is not in the document`);
    return found;
}

function typeAt(editor: Editor, position: number, text: string): void {
    editor.view.dispatch(editor.state.tr.insertText(text, position));
}

/** Where each top-level block of *source* starts and ends. */
function blockSpans(source: string): [number, number][] {
    const { blocks, gaps } = splitArticleSource(source);
    const spans: [number, number][] = [];
    let offset = 0;
    blocks.forEach((block, index) => {
        offset += gaps[index]!.length;
        spans.push([offset, offset + block.text.length]);
        offset += block.text.length;
    });
    return spans;
}

/** The part of *before* that *after* rewrote, as [start, end) offsets into *before*. */
function changedSpan(before: string, after: string): [number, number] {
    let start = 0;
    while (start < before.length && start < after.length && before[start] === after[start]) start += 1;
    let end = 0;
    while (end < before.length - start && end < after.length - start && before[before.length - 1 - end] === after[after.length - 1 - end]) end += 1;
    return [start, before.length - end];
}

function firstTextPosition(child: ProseMirrorNode, offset: number): number {
    let found = -1;
    child.descendants((node, pos) => {
        if (found < 0 && node.isText) found = offset + 1 + pos;
        return found < 0;
    });
    return found;
}

const MARK = "ZQZ";

describe("loading an article into the visual editor", () => {
    test.each(Object.entries(ARTICLE_FIXTURES))("leaves the %s article's source alone", async (_name, source) => {
        const { textarea, inputs } = await mount(source);
        expect(textarea.value).toBe(source);
        expect(inputs()).toBe(0);
    });

    test("shows footnote references as references, and source it can't edit as protected source", async () => {
        await mount(HAND_WRITTEN_ARTICLE);
        const canvas = document.querySelector("[data-article-canvas]")!;
        expect([...canvas.querySelectorAll("sup.footnote-ref")].map((sup) => sup.textContent)).toEqual(["[1]", "[note]", "[3]"]);
        const protectedBlocks = [...canvas.querySelectorAll<HTMLElement>(".article-raw-markdown")].map((el) => el.textContent);
        expect(protectedBlocks).toEqual([
            "# Hollow Creek Asylum",
            "Setext Heading\n==============",
            '<img src="https://img.example/raw.png" width="300">',
            '<div class="note">\n**Warning:** asbestos throughout.\n</div>',
            "<!-- editor note: verify dates -->",
            "[^1]: https://archive.example/opening-1889",
            "[^note]: Smith, J. *Asylums of New England* (2004), p. 12.\n    Continued on the next line.",
            "[^3]: First paragraph of the note.\n\n    Second paragraph, indented under the note.",
        ]);
        expect([...canvas.querySelectorAll(".article-raw-inline")].map((el) => el.textContent)).toEqual([
            "<kbd>",
            "</kbd>",
            "<kbd>",
            "</kbd>",
            "<sup>",
            "</sup>",
            "^[Unverified, from a forum post.]",
        ]);
    });
});

describe("protected source", () => {
    test.each([
        ["seeded", SEEDED_ARTICLE, []],
        ["wysiwyg", WYSIWYG_ARTICLE, ["[^1]: County archive, box 12."]],
    ])("the %s article is editable throughout, footnote definitions aside", async (_name, source, expected) => {
        await mount(source);
        expect([...document.querySelectorAll(".article-raw-markdown")].map((el) => el.textContent)).toEqual(expected);
    });
});

describe("the first edit", () => {
    const cases: [string, string, string][] = [
        ["seeded", SEEDED_ARTICLE, "The mill was built"],
        ["hand-written", HAND_WRITTEN_ARTICLE, "The asylum opened"],
        ["hand-written (inline HTML)", HAND_WRITTEN_ARTICLE, "Press "],
        ["wysiwyg", WYSIWYG_ARTICLE, "Built in 1902."],
    ];

    test.each(cases)("in the %s article changes only the typed text", async (_name, source, needle) => {
        const { editor, textarea } = await mount(source);
        typeAt(editor, positionOf(editor, needle), MARK);
        expect(textarea.value).toBe(source.replace(needle, MARK + needle));
    });

    test.each(Object.entries(ARTICLE_FIXTURES))("in any block of the %s article rewrites nothing outside that block, and renders as before", async (_name, source) => {
        const { editor } = await mount(source);
        const children: number[] = [];
        editor.state.doc.forEach((child, offset) => {
            if (child.type.name !== "markdownRawBlock" && firstTextPosition(child, offset) >= 0) children.push(offset);
        });
        expect(children.length).toBeGreaterThan(5);
        const spans = blockSpans(source);
        for (const offset of children) {
            const { editor: fresh, textarea } = await mount(source);
            const child = fresh.state.doc.nodeAt(offset)!;
            typeAt(fresh, firstTextPosition(child, offset), MARK);
            const after = textarea.value;
            const [start, end] = changedSpan(source, after);
            const label = `editing ${JSON.stringify(child.textContent.slice(0, 40))}`;
            expect(spans.some(([blockStart, blockEnd]) => blockStart <= start && end <= blockEnd), `${label} rewrote outside its block`).toBe(true);
            expect(comparableRendering(after, {}).replace(MARK, ""), `${label} changed how it renders`).toBe(comparableRendering(source, {}));
        }
    });
});

describe("later edits", () => {
    test("undoing every edit restores the exact source", async () => {
        const { editor, textarea } = await mount(HAND_WRITTEN_ARTICLE);
        typeAt(editor, positionOf(editor, "Snake_case_word"), MARK);
        typeAt(editor, positionOf(editor, "Final paragraph"), MARK);
        expect(textarea.value).not.toBe(HAND_WRITTEN_ARTICLE);
        editor.commands.undo();
        editor.commands.undo();
        expect(textarea.value).toBe(HAND_WRITTEN_ARTICLE);
    });

    test("deleting a block keeps its neighbours and the link definitions as written", async () => {
        const { editor, textarea } = await mount(HAND_WRITTEN_ARTICLE);
        const start = positionOf(editor, "The east wing was");
        const $start = editor.state.doc.resolve(start);
        editor.view.dispatch(editor.state.tr.delete($start.before(), $start.after()));
        const paragraph = HAND_WRITTEN_ARTICLE.slice(HAND_WRITTEN_ARTICLE.indexOf("The east wing"), HAND_WRITTEN_ARTICLE.indexOf("* Access"));
        expect(textarea.value).toBe(HAND_WRITTEN_ARTICLE.replace(paragraph, ""));
    });

    test("a new block typed after the last one is appended, keeping the source's final newline", async () => {
        const { editor, textarea } = await mount(WYSIWYG_ARTICLE);
        const end = editor.state.doc.content.size;
        editor.view.dispatch(editor.state.tr.insert(end, editor.schema.nodes.paragraph!.create(null, editor.schema.text("Added later."))));
        expect(textarea.value).toBe(WYSIWYG_ARTICLE.replace(/\n$/, "\n\nAdded later.\n"));
    });

    test("an image followed by more text stays a block of its own", async () => {
        const { editor, textarea } = await mount("Intro.\n");
        const { schema } = editor;
        const end = editor.state.doc.content.size;
        editor.view.dispatch(
            editor.state.tr.insert(end, [schema.nodes.image!.create({ src: "/media/image/1/", alt: "Hall" }), schema.nodes.paragraph!.create(null, schema.text("After."))]),
        );
        expect(textarea.value).toBe("Intro.\n\n![Hall](/media/image/1/)\n\nAfter.\n");
    });

    test("clearing the article empties the source", async () => {
        const { editor, textarea } = await mount(HAND_WRITTEN_ARTICLE);
        editor.commands.clearContent(true);
        expect(textarea.value).toBe("");
    });
});

describe("lists", () => {
    test("an edited list keeps its own markers, so it doesn't run into the different list after it", async () => {
        const source = "* one\n* two\n\n- three\n";
        const { editor, textarea } = await mount(source);
        typeAt(editor, positionOf(editor, "two"), MARK);
        expect(textarea.value).toBe(`* one\n* ${MARK}two\n\n- three\n`);
    });

    test("a new list beside another one gets a different marker, so they stay two lists", async () => {
        const { editor, textarea } = await mount("- a\n\nPara\n");
        const { schema } = editor;
        const list = schema.nodes.bulletList!.create(null, schema.nodes.listItem!.create(null, schema.nodes.paragraph!.create(null, schema.text("new"))));
        editor.view.dispatch(editor.state.tr.insert(editor.state.doc.firstChild!.nodeSize, list));
        expect(textarea.value).toBe("- a\n\n* new\n\nPara\n");
    });

    test("a list that would swallow the indented code after it is kept apart by a comment", async () => {
        const { editor, textarea } = await mount("Para\n\n    code\n");
        const { schema } = editor;
        const list = schema.nodes.bulletList!.create(null, schema.nodes.listItem!.create(null, schema.nodes.paragraph!.create(null, schema.text("Para"))));
        editor.view.dispatch(editor.state.tr.replaceWith(0, editor.state.doc.firstChild!.nodeSize, list));
        expect(textarea.value).toBe("- Para\n\n<!-- -->\n\n    code\n");
        expect(comparableRendering(textarea.value, {})).toContain("<pre><code>code");
    });
});

describe("switching modes", () => {
    const toggle = (): void => document.querySelector<HTMLElement>("[data-article-mode-toggle]")!.click();

    /** Count the editor's Markdown parses, optionally rewriting what each one is given. */
    function spyOnParse(editor: Editor, rewrite: (text: string) => string = (text) => text): () => number {
        const { parser } = editor.storage.markdown;
        const parse = parser.parse.bind(parser);
        let calls = 0;
        parser.parse = (content, options) => {
            calls += 1;
            return parse(rewrite(content), options);
        };
        return () => calls;
    }

    test("loads every editable block in one parse", async () => {
        const { editor } = await mount(HAND_WRITTEN_ARTICLE);
        const calls = spyOnParse(editor);
        toggle();
        toggle();
        expect(calls()).toBe(1);
    });

    test("parses each block alone when a single parse can't be split back into blocks", async () => {
        const { editor, textarea } = await mount(HAND_WRITTEN_ARTICLE);
        const before = editor.state.doc;
        const calls = spyOnParse(editor, (text) => text.replaceAll("<!-- urbanlens:block -->", ""));
        toggle();
        toggle();
        expect(calls()).toBeGreaterThan(1);
        expect(editor.state.doc.eq(before)).toBe(true);
        typeAt(editor, positionOf(editor, "The asylum opened"), MARK);
        expect(textarea.value).toBe(HAND_WRITTEN_ARTICLE.replace("The asylum opened", `${MARK}The asylum opened`));
    });

    test("returning from Source mode keeps hand-edited source as typed", async () => {
        const { editor, textarea, inputs } = await mount(SEEDED_ARTICLE);
        toggle();
        const edited = `${SEEDED_ARTICLE}\n\n* a *starred* list\n* written by hand\n`;
        textarea.value = edited;
        textarea.dispatchEvent(new Event("input", { bubbles: true }));
        const before = inputs();
        toggle();
        expect(textarea.value).toBe(edited);
        expect(inputs()).toBe(before);

        typeAt(editor, positionOf(editor, "The mill was built"), MARK);
        expect(textarea.value).toBe(edited.replace("The mill was built", `${MARK}The mill was built`));
    });
});

describe("inserting a reference", () => {
    test("writes the marker unescaped and adds its definition", async () => {
        const { editor, textarea } = await mount(WYSIWYG_ARTICLE);
        editor.commands.setTextSelection(positionOf(editor, "First line") + "First line".length);
        document.querySelector<HTMLElement>('[data-md-action="reference"]')!.click();
        expect(textarea.value).toBe(WYSIWYG_ARTICLE.replace("First line\\", "First line[^2]\\") + "\n[^2]: ");
    });
});
