/**
 * Article Markdown syntax that the WYSIWYG editor's parser lacks but the server renderer
 * (`services/wiki/articles.py`: markdown-it-py "gfm-like" with raw HTML and footnotes) supports, plus the
 * block splitting and rendering comparison the editor uses to keep saved source intact.
 */

import MarkdownIt from "markdown-it";
import { escHtml } from "./escape-html";

type RuleBlock = Parameters<MarkdownIt["block"]["ruler"]["before"]>[2];
type RuleInline = Parameters<MarkdownIt["inline"]["ruler"]["before"]>[2];
type RuleCore = Parameters<MarkdownIt["core"]["ruler"]["before"]>[2];
type StateBlock = Parameters<RuleBlock>[0];
type StateInline = Parameters<RuleInline>[0];

/** The parse-wide state a block needs from the rest of its article: link reference and footnote definitions. */
export interface ArticleEnv {
    references?: Record<string, unknown>;
    footnoteLabels?: Set<string>;
}

export interface ArticleSyntaxOptions {
    /** Treat every `[^label]` as a footnote reference, defined or not; the server only matches defined labels. */
    alwaysMatchFootnoteRefs?: boolean;
}

/** Token types this plugin adds. */
export const FOOTNOTE_DEF = "article_footnote_def";
export const FOOTNOTE_REF = "article_footnote_ref";
export const RAW_INLINE = "article_raw_inline";

const ATTR_NAME = "[a-zA-Z_:][a-zA-Z0-9:._-]*";
const ATTR_VALUE = "(?:[^\"'=<>`\\x00-\\x20]+|'[^']*'|\"[^\"]*\")";
const ATTRIBUTE = `(?:\\s+${ATTR_NAME}(?:\\s*=\\s*${ATTR_VALUE})?)`;
/** markdown-it's own inline HTML pattern (lib/common/html_re.mjs), for parsers with `html: false`. */
const HTML_TAG_RE = new RegExp(
    "^(?:" +
        [
            `<[A-Za-z][A-Za-z0-9\\-]*${ATTRIBUTE}*\\s*\\/?>`,
            "<\\/[A-Za-z][A-Za-z0-9\\-]*\\s*>",
            "<!---?>|<!--(?:[^-]|-[^-]|--[^>])*-->",
            "<[?][\\s\\S]*?[?]>",
            "<![A-Za-z][^>]*>",
            "<!\\[CDATA\\[[\\s\\S]*?\\]\\]>",
        ].join("|") +
        ")",
);

function footnoteLabels(env: ArticleEnv): Set<string> {
    env.footnoteLabels ??= new Set();
    return env.footnoteLabels;
}

/** A copy a parse can add definitions to without leaking them into *env*. */
export function cloneEnv(env: ArticleEnv): ArticleEnv {
    return { references: env.references ? { ...env.references } : undefined, footnoteLabels: new Set(env.footnoteLabels) };
}

/**
 * `[^label]: text` with its indented continuation, measured the way markdown-it-footnote (and its Python port the
 * server uses) measures it. Emitted as one token holding the definition's source.
 */
const footnoteDef: RuleBlock = (state: StateBlock, startLine: number, endLine: number, silent: boolean): boolean => {
    const start = state.bMarks[startLine]! + state.tShift[startLine]!;
    const max = state.eMarks[startLine]!;
    if (start + 4 > max) return false;
    if (state.src.charCodeAt(start) !== 0x5b || state.src.charCodeAt(start + 1) !== 0x5e) return false;
    let pos = start + 2;
    for (; pos < max; pos++) {
        const ch = state.src.charCodeAt(pos);
        if (ch === 0x20) return false;
        if (ch === 0x5d) break;
    }
    if (pos === start + 2) return false;
    pos += 1;
    if (pos >= max || state.src.charCodeAt(pos) !== 0x3a) return false;
    if (silent) return true;
    pos += 1;
    const env: ArticleEnv = state.env;
    footnoteLabels(env).add(state.src.slice(start + 2, pos - 2));

    const oldBMark = state.bMarks[startLine]!;
    const oldTShift = state.tShift[startLine]!;
    const oldSCount = state.sCount[startLine]!;
    const posAfterColon = pos;
    const initial = oldSCount + pos - (oldBMark + oldTShift);
    let offset = initial;
    while (pos < max) {
        const ch = state.src.charCodeAt(pos);
        if (ch === 0x09) offset += 4 - (offset % 4);
        else if (ch === 0x20) offset += 1;
        else break;
        pos += 1;
    }
    state.tShift[startLine] = pos - posAfterColon;
    state.sCount[startLine] = offset - initial;
    state.bMarks[startLine] = posAfterColon;
    state.blkIndent += 4;
    if (state.sCount[startLine]! < state.blkIndent) state.sCount[startLine] = state.sCount[startLine]! + state.blkIndent;

    // Tokenized only to find where the definition ends; its content is kept as source.
    const tokenCount = state.tokens.length;
    state.md.block.tokenize(state, startLine, endLine);
    state.tokens.length = tokenCount;

    state.blkIndent -= 4;
    state.tShift[startLine] = oldTShift;
    state.sCount[startLine] = oldSCount;
    state.bMarks[startLine] = oldBMark;

    let last = state.line - 1;
    while (last > startLine && state.isEmpty(last)) last -= 1;
    const token = state.push(FOOTNOTE_DEF, "", 0);
    token.map = [startLine, state.line];
    token.block = true;
    token.content = state.src.slice(oldBMark, state.eMarks[last]);
    return true;
};

/** `^[inline footnote]`, kept as source. */
const footnoteInline: RuleInline = (state: StateInline, silent: boolean): boolean => {
    const start = state.pos;
    if (start + 2 >= state.posMax) return false;
    if (state.src.charCodeAt(start) !== 0x5e || state.src.charCodeAt(start + 1) !== 0x5b) return false;
    const labelEnd = state.md.helpers.parseLinkLabel(state, start + 1);
    if (labelEnd < 0) return false;
    if (!silent) state.push(RAW_INLINE, "", 0).content = state.src.slice(start, labelEnd + 1);
    state.pos = labelEnd + 1;
    return true;
};

function footnoteRef(alwaysMatch: boolean): RuleInline {
    return (state: StateInline, silent: boolean): boolean => {
        const start = state.pos;
        const max = state.posMax;
        if (start + 3 > max) return false;
        if (state.src.charCodeAt(start) !== 0x5b || state.src.charCodeAt(start + 1) !== 0x5e) return false;
        let pos = start + 2;
        for (; pos < max; pos++) {
            const ch = state.src.charCodeAt(pos);
            if (ch === 0x20 || ch === 0x0a) return false;
            if (ch === 0x5d) break;
        }
        if (pos === start + 2 || pos >= max) return false;
        const label = state.src.slice(start + 2, pos);
        const env: ArticleEnv = state.env;
        if (!alwaysMatch && !footnoteLabels(env).has(label)) return false;
        if (!silent) state.push(FOOTNOTE_REF, "", 0).meta = { label };
        state.pos = pos + 1;
        return true;
    };
}

/** Inline HTML for a parser that has raw HTML turned off, which would otherwise read it as text. */
const rawHtmlInline: RuleInline = (state: StateInline, silent: boolean): boolean => {
    if (state.md.options.html) return false;
    const pos = state.pos;
    if (state.src.charCodeAt(pos) !== 0x3c || pos + 2 >= state.posMax) return false;
    const match = state.src.slice(pos, state.posMax).match(HTML_TAG_RE);
    if (!match) return false;
    if (!silent) state.push(RAW_INLINE, "", 0).content = match[0];
    state.pos += match[0].length;
    return true;
};

let injectedEnv: ArticleEnv | null = null;

/** Seeds every parse with the definitions of the article a block was cut from (see {@link withArticleEnv}). */
const injectEnv: RuleCore = (state) => {
    if (injectedEnv) Object.assign(state.env, cloneEnv(injectedEnv));
};

/**
 * Run *parse* with *env*'s definitions visible to every markdown-it parse inside it, for parsers whose callers
 * don't take an env (tiptap-markdown's).
 */
export function withArticleEnv<T>(env: ArticleEnv, parse: () => T): T {
    injectedEnv = env;
    try {
        return parse();
    } finally {
        injectedEnv = null;
    }
}

const installed = new WeakSet<MarkdownIt>();

/** markdown-it plugin: the article syntax above. Safe to apply to the same instance more than once. */
export function articleSyntax(md: MarkdownIt, options: ArticleSyntaxOptions = {}): void {
    if (installed.has(md)) return;
    installed.add(md);
    md.core.ruler.after("normalize", "article_env", injectEnv);
    md.block.ruler.before("reference", "article_footnote_def", footnoteDef, { alt: ["paragraph", "reference"] });
    md.inline.ruler.after("image", "article_footnote_inline", footnoteInline);
    md.inline.ruler.after("article_footnote_inline", "article_footnote_ref", footnoteRef(options.alwaysMatchFootnoteRefs ?? false));
    md.inline.ruler.after("html_inline", "article_raw_html_inline", rawHtmlInline);
    md.renderer.rules[FOOTNOTE_DEF] = (tokens, idx) => `<div data-md-raw-block="${escHtml(tokens[idx]!.content)}"></div>\n`;
    md.renderer.rules[FOOTNOTE_REF] = (tokens, idx) => `<sup class="footnote-ref" data-footnote-ref="${escHtml(tokens[idx]!.meta?.label)}"></sup>`;
    md.renderer.rules[RAW_INLINE] = (tokens, idx) => `<span data-md-raw-inline="${escHtml(tokens[idx]!.content)}"></span>`;
}

let referenceInstance: MarkdownIt | null = null;

/** A parser with the server renderer's syntax: raw HTML, linkify, tables, strikethrough and footnotes. */
function referenceMarkdown(): MarkdownIt {
    referenceInstance ??= new MarkdownIt({ html: true, linkify: true }).use(articleSyntax);
    return referenceInstance;
}

/** One top-level block of article source; `raw` when the WYSIWYG editor has no node for it. */
export interface SourceBlock {
    text: string;
    raw: boolean;
}

/**
 * Article source cut at its top-level blocks. `gaps[i]` is the text before `blocks[i]` and the last gap is the text
 * after the final block, so interleaving them reproduces the source exactly. Gaps hold blank lines and link reference
 * definitions, which have no block of their own.
 */
export interface SplitSource {
    blocks: SourceBlock[];
    gaps: string[];
    env: ArticleEnv;
}

const RAW_BLOCK_TYPES = new Set(["html_block", FOOTNOTE_DEF]);
const LINE_BREAK = /\r\n?|\n/g;

/** Split *source* at its top-level blocks as the server's parser sees them. */
export function splitArticleSource(source: string): SplitSource {
    const env: ArticleEnv = {};
    const tokens = referenceMarkdown().parse(source, env);

    const lineStarts = [0];
    const lineEnds: number[] = [];
    for (const match of source.matchAll(LINE_BREAK)) {
        lineEnds.push(match.index);
        lineStarts.push(match.index + match[0].length);
    }
    lineEnds.push(source.length);
    const isBlank = (line: number): boolean => /^[ \t]*$/.test(source.slice(lineStarts[line], lineEnds[line]));

    const blocks: SourceBlock[] = [];
    const gaps: string[] = [];
    let cursor = 0;
    for (const token of tokens) {
        if (token.level !== 0 || token.nesting < 0 || !token.map) continue;
        const [first, end] = token.map;
        let last = Math.min(end, lineEnds.length) - 1;
        while (last > first && isBlank(last)) last -= 1;
        const start = lineStarts[first]!;
        const stop = lineEnds[last]!;
        gaps.push(source.slice(cursor, start));
        blocks.push({ text: source.slice(start, stop), raw: RAW_BLOCK_TYPES.has(token.type) });
        cursor = stop;
    }
    gaps.push(source.slice(cursor));
    return { blocks, gaps, env };
}

export interface ListMarker {
    kind: "bullet" | "ordered";
    /** The bullet character, or the delimiter after an ordered list's numbers. */
    marker: string;
}

/** The marker *block* starts with, when it is a list. */
export function listMarkerOf(block: string): ListMarker | null {
    const first = referenceMarkdown().parse(block, {})[0];
    if (first?.type === "bullet_list_open") return { kind: "bullet", marker: first.markup };
    if (first?.type === "ordered_list_open") return { kind: "ordered", marker: first.markup };
    return null;
}

/** *block*, a single list, with each of its own items' markers (not nested lists') changed to *marker*. */
export function withListMarker(block: string, marker: string): string {
    const lines = block.split("\n");
    for (const token of referenceMarkdown().parse(block, {})) {
        if (token.type !== "list_item_open" || token.level !== 1 || !token.map) continue;
        const line = token.map[0];
        lines[line] = lines[line]!.replace(/^( {0,3}\d{0,9})[-*+.)]/, `$1${marker}`);
    }
    return lines.join("\n");
}

/**
 * *markdown* rendered for comparison: two sources that compare equal render the same article. Whitespace outside
 * code blocks is collapsed, and inline styles (which the server's sanitizer strips) are dropped.
 */
export function comparableRendering(markdown: string, env: ArticleEnv): string {
    const html = referenceMarkdown().render(markdown, cloneEnv(env));
    return html
        .split(/(<pre[\s\S]*?<\/pre>)/)
        .map((part) => (part.startsWith("<pre") ? part : part.replace(/ style="[^"]*"/g, "").replace(/[ \t\r\n]+/g, " ")))
        .join("")
        .trim();
}
