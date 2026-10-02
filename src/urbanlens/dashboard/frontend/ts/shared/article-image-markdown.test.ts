import { expect, test } from "bun:test";
import { Schema } from "@tiptap/pm/model";
import MarkdownIt from "markdown-it";
import { blockImageMarkdown } from "./article-source";

const schema = new Schema({
    nodes: {
        doc: { content: "block+" },
        image: { group: "block", attrs: { src: { default: null }, alt: { default: null }, title: { default: null } } },
        text: {},
    },
});
const md = new MarkdownIt();

function written(attrs: { src: string; alt?: string; title?: string }): string {
    let out = "";
    const state = { write: (content = "") => void (out += content), text: () => {}, esc: (text: string) => text, closeBlock: () => {} };
    blockImageMarkdown.serialize(state, schema.nodes.image!.create(attrs));
    return out;
}

function parsed(markdown: string): { src: string | null; title: string | null } {
    const image = md.parseInline(markdown, {})[0]?.children?.find((token) => token.type === "image");
    return { src: image?.attrGet("src") ?? null, title: image?.attrGet("title") ?? null };
}

test.each([
    ["a plain image", "/media/a.jpg", "Front gate"],
    ["parentheses in the src", "/media/a(1).jpg", ""],
    ["a backslash ending the src", "/media/a\\", ""],
    ["a backslash before a parenthesis", "/media/a\\(1).jpg", ""],
    ["a space in the src", "/media/old mill.jpg", ""],
    ["a newline in the src", "/media/a\nb.jpg", ""],
    ["angle brackets in the src", "<x>/media/a>.jpg", ""],
    ["a quote in the title", "/media/a.jpg", 'The "red" door'],
    ["a backslash ending the title", "/media/a.jpg", "C:\\"],
    ["a backslash before a quote in the title", "/media/a.jpg", 'say \\"hi\\"'],
])("%s survives a round trip", (_name, src, title) => {
    expect(parsed(written({ src, title }))).toEqual({ src: md.normalizeLink(src), title: title || null });
});
