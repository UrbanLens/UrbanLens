import { describe, expect, test } from "bun:test";
import { ARTICLE_FIXTURES, HAND_WRITTEN_ARTICLE } from "../testing/article-fixtures";
import { comparableRendering, listMarkerOf, splitArticleSource, withListMarker } from "./article-markdown-syntax";

function reassemble(source: string): string {
    const { blocks, gaps } = splitArticleSource(source);
    return blocks.map((block, index) => gaps[index] + block.text).join("") + gaps[gaps.length - 1];
}

describe("splitArticleSource", () => {
    test.each([...Object.entries(ARTICLE_FIXTURES), ["empty", ""], ["blank", "\n\n"], ["definitions only", "[a]: https://a.example\n"]])(
        "gaps and blocks reproduce %s exactly",
        (_name, source) => {
            expect(reassemble(source)).toBe(source);
        },
    );

    test("cuts at the server renderer's top-level blocks", () => {
        // Line ranges from markdown-it-py "gfm-like" + footnotes (services/wiki/articles.py) over the same text.
        const lines = HAND_WRITTEN_ARTICLE.split("\n");
        const serverFirstLines = [0, 2, 5, 8, 12, 18, 21, 25, 30, 32, 34, 36, 40, 42, 44, 46, 51, 55, 57, 59, 61, 63, 66, 68, 74, 76, 78].map((line) => lines[line]);
        const { blocks } = splitArticleSource(HAND_WRITTEN_ARTICLE);
        const firstLines = blocks.filter((block) => !block.text.startsWith("[^")).map((block) => block.text.split("\n")[0]);
        expect(firstLines).toEqual(serverFirstLines);
    });

    test("raw HTML blocks and footnote definitions are raw; everything else is not", () => {
        const raw = splitArticleSource(HAND_WRITTEN_ARTICLE)
            .blocks.filter((block) => block.raw)
            .map((block) => block.text);
        expect(raw).toEqual([
            '<img src="https://img.example/raw.png" width="300">',
            '<div class="note">\n**Warning:** asbestos throughout.\n</div>',
            "<!-- editor note: verify dates -->",
            "[^1]: https://archive.example/opening-1889",
            "[^note]: Smith, J. *Asylums of New England* (2004), p. 12.\n    Continued on the next line.",
            "[^3]: First paragraph of the note.\n\n    Second paragraph, indented under the note.",
        ]);
    });

    test("link reference definitions stay in the gaps", () => {
        const { gaps } = splitArticleSource(HAND_WRITTEN_ARTICLE);
        expect(gaps.join("")).toContain('[county]: https://county.example/records/123 "County records"\n[east]: https://img.example/east.jpg');
    });

    test("a footnote definition ends where the server's does", () => {
        expect(splitArticleSource("[^a]: x\n\nNot indented\n\nuse[^a]").blocks.map((block) => block.text)).toEqual(["[^a]: x", "Not indented", "use[^a]"]);
        // It interrupts a paragraph, and an unindented line straight after it is a lazy continuation.
        expect(splitArticleSource("Para\n[^b]: interrupts\nuse[^b]").blocks.map((block) => block.text)).toEqual(["Para", "[^b]: interrupts\nuse[^b]"]);
    });

    test("a block's trailing blank lines belong to the gap after it", () => {
        const { blocks, gaps } = splitArticleSource("- a\n- b\n\n\n\nnext\n");
        expect(blocks.map((block) => block.text)).toEqual(["- a\n- b", "next"]);
        expect(gaps).toEqual(["", "\n\n\n\n", "\n"]);
    });
});

describe("list markers", () => {
    test("reads a list's marker", () => {
        expect(listMarkerOf("* a\n* b")).toEqual({ kind: "bullet", marker: "*" });
        expect(listMarkerOf("3) a")).toEqual({ kind: "ordered", marker: ")" });
        expect(listMarkerOf("A paragraph")).toBeNull();
    });

    test("rewrites only the list's own items, not nested lists or continuation lines", () => {
        expect(withListMarker("- a\n  - nested\n  more\n- b", "*")).toBe("* a\n  - nested\n  more\n* b");
        expect(withListMarker(" 9. a\n10. b", ")")).toBe(" 9) a\n10) b");
    });
});

describe("comparableRendering", () => {
    test("a footnote reference is one only when its definition exists, as on the server", () => {
        expect(comparableRendering("a[^1]", {})).toBe("<p>a[^1]</p>");
        expect(comparableRendering("a[^1]", { footnoteLabels: new Set(["1"]) })).toContain("data-footnote-ref");
    });

    test("an inline footnote is its own token, so writing it as text would change the rendering", () => {
        expect(comparableRendering("note^[a source] here", {})).not.toBe(comparableRendering("note^\\[a source\\] here", {}));
    });

    test("raw HTML renders as HTML, so escaping it would change the rendering", () => {
        expect(comparableRendering("Press <kbd>Ctrl</kbd>", {})).not.toBe(comparableRendering("Press &lt;kbd&gt;Ctrl&lt;/kbd&gt;", {}));
    });

    test("ignores differences the article cannot show: soft-wrapped lines and table alignment", () => {
        expect(comparableRendering("a\nb", {})).toBe(comparableRendering("a b", {}));
        expect(comparableRendering("| a |\n|--:|\n| 1 |", {})).toBe(comparableRendering("| a |\n| --- |\n| 1 |", {}));
    });

    test("keeps whitespace inside code blocks", () => {
        expect(comparableRendering("```\na  b\n```", {})).not.toBe(comparableRendering("```\na b\n```", {}));
    });
});
