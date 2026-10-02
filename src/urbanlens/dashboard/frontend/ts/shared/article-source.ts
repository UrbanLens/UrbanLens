/**
 * Keeps an article's saved Markdown intact while it is edited in the WYSIWYG canvas.
 *
 * The canvas's document model can't hold everything the server renders (raw HTML, footnote definitions, the exact
 * spelling of a list marker), so serializing the whole document rewrites source the user never touched. Instead the
 * source is cut into top-level blocks, each block remembers the nodes it parsed to, and serializing writes a block's
 * original text for as long as its nodes are unchanged. Only blocks the user edits are written from the model, and a
 * block whose model would not render the same as its source is shown as protected source rather than editable nodes.
 */

import { createDocument, type Editor, mergeAttributes, Node } from "@tiptap/core";
import type MarkdownIt from "markdown-it";
import type { Node as ProseMirrorNode } from "@tiptap/pm/model";
import {
    type ArticleEnv,
    articleSyntax,
    comparableRendering,
    type ListMarker,
    listMarkerOf,
    type SplitSource,
    splitArticleSource,
    withArticleEnv,
    withListMarker,
} from "./article-markdown-syntax";

/** The parts of tiptap-markdown's storage this module uses. */
export interface TiptapMarkdownStorage {
    getMarkdown(): string;
    parser: { parse(content: string, options?: { inline?: boolean }): string };
    serializer: { serialize(content: ProseMirrorNode): string };
}

// tiptap-markdown ships its own MarkdownStorage type but doesn't augment @tiptap/core's Storage interface itself.
declare module "@tiptap/core" {
    interface Storage {
        markdown: TiptapMarkdownStorage;
    }
}

/** The prosemirror-markdown serializer state methods node serializers here call. */
interface SerializerState {
    write(content?: string): void;
    text(text: string, escape?: boolean): void;
    esc(text: string, startOfLine?: boolean): string;
    closeBlock(node: ProseMirrorNode): void;
}

/**
 * Markdown for the article's block-level image node. tiptap-markdown's default writes it like an inline image and
 * never closes the block, gluing the next block onto the image's line.
 */
export const blockImageMarkdown = {
    serialize(state: SerializerState, node: ProseMirrorNode) {
        const title = node.attrs.title ? ` "${String(node.attrs.title).replace(/"/g, '\\"')}"` : "";
        state.write(`![${state.esc(String(node.attrs.alt ?? ""))}](${String(node.attrs.src ?? "").replace(/[()]/g, "\\$&")}${title})`);
        state.closeBlock(node);
    },
    parse: {},
};

const SOURCE_MODE_HINT = "Edit this in Source mode";

function sourceAttribute(name: string) {
    return {
        source: {
            default: "",
            parseHTML: (element: HTMLElement) => element.getAttribute(name) ?? "",
            renderHTML: (attributes: Record<string, unknown>) => ({ [name]: String(attributes.source ?? "") }),
        },
    };
}

/** A top-level block of source the canvas can't edit faithfully, shown as its source and written back verbatim. */
export const MarkdownRawBlock = Node.create({
    name: "markdownRawBlock",
    group: "block",
    atom: true,
    selectable: true,
    draggable: false,

    addAttributes() {
        return sourceAttribute("data-md-raw-block");
    },

    parseHTML() {
        return [{ tag: "div[data-md-raw-block]", priority: 60 }];
    },

    renderHTML({ node, HTMLAttributes }) {
        return [
            "div",
            mergeAttributes(HTMLAttributes, { class: "article-raw-markdown", title: SOURCE_MODE_HINT }),
            ["pre", ["code", String(node.attrs.source ?? "")]],
        ];
    },

    addStorage() {
        return {
            markdown: {
                serialize(state: SerializerState, node: ProseMirrorNode) {
                    state.text(String(node.attrs.source ?? ""), false);
                    state.closeBlock(node);
                },
                parse: {},
            },
        };
    },
});

/** Inline source the canvas can't edit faithfully (an HTML tag, an inline footnote), written back verbatim. */
export const MarkdownRawInline = Node.create({
    name: "markdownRawInline",
    group: "inline",
    inline: true,
    atom: true,
    selectable: true,

    addAttributes() {
        return sourceAttribute("data-md-raw-inline");
    },

    parseHTML() {
        return [{ tag: "[data-md-raw-inline]", priority: 60 }];
    },

    renderHTML({ node, HTMLAttributes }) {
        return [
            "code",
            mergeAttributes(HTMLAttributes, { class: "article-raw-inline", title: SOURCE_MODE_HINT }),
            String(node.attrs.source ?? ""),
        ];
    },

    addStorage() {
        return {
            markdown: {
                serialize(state: SerializerState, node: ProseMirrorNode) {
                    state.text(String(node.attrs.source ?? ""), false);
                },
                parse: {},
            },
        };
    },
});

/** A `[^label]` footnote reference. Its definition stays in the source as a raw block. */
export const FootnoteReference = Node.create({
    name: "footnoteReference",
    group: "inline",
    inline: true,
    atom: true,
    selectable: true,

    addAttributes() {
        return {
            label: {
                default: "",
                parseHTML: (element: HTMLElement) => element.getAttribute("data-footnote-ref") ?? "",
                renderHTML: (attributes: Record<string, unknown>) => ({ "data-footnote-ref": String(attributes.label ?? "") }),
            },
        };
    },

    parseHTML() {
        return [{ tag: "sup[data-footnote-ref]", priority: 60 }];
    },

    renderHTML({ node, HTMLAttributes }) {
        return ["sup", mergeAttributes(HTMLAttributes, { class: "footnote-ref" }), ["a", `[${String(node.attrs.label ?? "")}]`]];
    },

    addStorage() {
        return {
            markdown: {
                serialize(state: SerializerState, node: ProseMirrorNode) {
                    state.write(`[^${String(node.attrs.label ?? "")}]`);
                },
                parse: {
                    setup(md: MarkdownIt) {
                        md.use(articleSyntax, { alwaysMatchFootnoteRefs: true });
                        // tiptap-markdown strips a newline that follows any element, which glued "**a**\nb" into "ab".
                        md.renderer.rules.softbreak = () => " ";
                    },
                },
            },
        };
    },
});

export const ARTICLE_SOURCE_EXTENSIONS = [MarkdownRawBlock, MarkdownRawInline, FootnoteReference];

interface BaselineBlock {
    text: string;
    nodes: readonly ProseMirrorNode[];
}

interface Baseline {
    blocks: readonly BaselineBlock[];
    gaps: readonly string[];
}

const LIST_MARKERS: Record<ListMarker["kind"], readonly string[]> = { bullet: ["-", "*", "+"], ordered: [".", ")"] };

/** Renders nothing, and ends whatever block precedes it. */
const BLOCK_BREAK = "<!-- -->";

/** Separates blocks parsed together; see {@link ArticleSourceTracker.parseBlocks}. */
const PARSE_BREAK = "<!-- urbanlens:block -->";

/** Cells above which a stretch of blocks is aligned on its unique blocks before it is diffed exactly. */
const MAX_DIFF_CELLS = 1_000_000;

const nodeKeys = new WeakMap<ProseMirrorNode, string>();

/** A string equal for equal nodes. Nodes are immutable, and an unchanged block keeps its node, so each is keyed once. */
function keyOf(node: ProseMirrorNode): string {
    let key = nodeKeys.get(node);
    if (key === undefined) {
        key = JSON.stringify(node.toJSON());
        nodeKeys.set(node, key);
    }
    return key;
}

function groupMatches(group: readonly ProseMirrorNode[], children: readonly ProseMirrorNode[], at: number, end: number): boolean {
    if (at < 0 || at + group.length > end) return false;
    return group.every((node, offset) => {
        const child = children[at + offset]!;
        return child === node || (child.type === node.type && child.nodeSize === node.nodeSize && child.eq(node));
    });
}

/**
 * Pair original blocks with the runs of *children* still equal to them, keeping the most blocks in order.
 *
 * Returns the child index each block starts at, or -1 for a block that is gone or changed.
 */
export function alignBlocks(groups: readonly (readonly ProseMirrorNode[])[], children: readonly ProseMirrorNode[]): number[] {
    const starts = groups.map(() => -1);
    alignRange(groups, children, [0, groups.length], [0, children.length], starts);
    return starts;
}

type Range = [start: number, end: number];

function alignRange(groups: readonly (readonly ProseMirrorNode[])[], children: readonly ProseMirrorNode[], blocks: Range, kids: Range, starts: number[]): void {
    let [firstBlock, lastBlock] = blocks;
    let [firstChild, lastChild] = kids;
    while (firstBlock < lastBlock && groupMatches(groups[firstBlock]!, children, firstChild, lastChild)) {
        starts[firstBlock] = firstChild;
        firstChild += groups[firstBlock]!.length;
        firstBlock += 1;
    }
    while (lastBlock > firstBlock) {
        const group = groups[lastBlock - 1]!;
        const at = lastChild - group.length;
        if (at < firstChild || !groupMatches(group, children, at, lastChild)) break;
        starts[lastBlock - 1] = at;
        lastChild = at;
        lastBlock -= 1;
    }
    if (firstBlock === lastBlock || firstChild === lastChild) return;
    if ((lastBlock - firstBlock + 1) * (lastChild - firstChild + 1) <= MAX_DIFF_CELLS) {
        diffExactly(groups, children, [firstBlock, lastBlock], [firstChild, lastChild], starts);
        return;
    }
    const anchors = uniqueAnchors(groups, children, [firstBlock, lastBlock], [firstChild, lastChild]);
    if (!anchors.length) {
        matchInOrder(groups, children, [firstBlock, lastBlock], [firstChild, lastChild], starts);
        return;
    }
    let block = firstBlock;
    let child = firstChild;
    for (const [anchorBlock, anchorChild] of anchors) {
        alignRange(groups, children, [block, anchorBlock], [child, anchorChild], starts);
        starts[anchorBlock] = anchorChild;
        block = anchorBlock + 1;
        child = anchorChild + groups[anchorBlock]!.length;
    }
    alignRange(groups, children, [block, lastBlock], [child, lastChild], starts);
}

/** The longest common subsequence of blocks and children, by dynamic programming. */
function diffExactly(groups: readonly (readonly ProseMirrorNode[])[], children: readonly ProseMirrorNode[], blocks: Range, kids: Range, starts: number[]): void {
    const [firstBlock, lastBlock] = blocks;
    const [firstChild, lastChild] = kids;
    const blockCount = lastBlock - firstBlock;
    const childCount = lastChild - firstChild;
    // best[b * width + c]: the most blocks matchable from block firstBlock + b and child firstChild + c onward.
    const width = childCount + 1;
    const best = new Uint32Array((blockCount + 1) * width);
    for (let b = blockCount - 1; b >= 0; b--) {
        const group = groups[firstBlock + b]!;
        for (let c = childCount - 1; c >= 0; c--) {
            let value = Math.max(best[(b + 1) * width + c]!, best[b * width + c + 1]!);
            if (groupMatches(group, children, firstChild + c, lastChild)) value = Math.max(value, 1 + best[(b + 1) * width + c + group.length]!);
            best[b * width + c] = value;
        }
    }
    let b = 0;
    let c = 0;
    while (b < blockCount && c < childCount) {
        const here = best[b * width + c]!;
        if (here === best[(b + 1) * width + c]) b += 1;
        else if (here === best[b * width + c + 1]) c += 1;
        else {
            starts[firstBlock + b] = firstChild + c;
            c += groups[firstBlock + b]!.length;
            b += 1;
        }
    }
}

/**
 * Blocks whose content occurs once among the blocks and once among the children, paired up and cut down to the
 * longest run that is in order on both sides (patience diff's anchors).
 */
function uniqueAnchors(groups: readonly (readonly ProseMirrorNode[])[], children: readonly ProseMirrorNode[], blocks: Range, kids: Range): [number, number][] {
    const [firstBlock, lastBlock] = blocks;
    const [firstChild, lastChild] = kids;
    const blockCounts = new Map<string, number>();
    for (let block = firstBlock; block < lastBlock; block++) {
        const key = keyOf(groups[block]![0]!);
        blockCounts.set(key, (blockCounts.get(key) ?? 0) + 1);
    }
    const childAt = new Map<string, number>();
    const childCounts = new Map<string, number>();
    for (let child = firstChild; child < lastChild; child++) {
        const key = keyOf(children[child]!);
        childAt.set(key, child);
        childCounts.set(key, (childCounts.get(key) ?? 0) + 1);
    }
    const pairs: [number, number][] = [];
    for (let block = firstBlock; block < lastBlock; block++) {
        const key = keyOf(groups[block]![0]!);
        const child = childAt.get(key);
        if (child === undefined || blockCounts.get(key) !== 1 || childCounts.get(key) !== 1) continue;
        if (groupMatches(groups[block]!, children, child, lastChild)) pairs.push([block, child]);
    }
    // Longest run of pairs whose children increase too: tails[k] is the pair ending the best run of length k + 1.
    const tails: number[] = [];
    const previous = pairs.map(() => -1);
    pairs.forEach(([, child], index) => {
        let low = 0;
        let high = tails.length;
        while (low < high) {
            const middle = (low + high) >> 1;
            if (pairs[tails[middle]!]![1] < child) low = middle + 1;
            else high = middle;
        }
        if (low > 0) previous[index] = tails[low - 1]!;
        tails[low] = index;
    });
    const run: [number, number][] = [];
    for (let index = tails[tails.length - 1] ?? -1; index >= 0; index = previous[index]!) run.unshift(pairs[index]!);
    // A block of several nodes can reach past the next anchor's child; that anchor is dropped.
    const anchors: [number, number][] = [];
    for (const pair of run) {
        const last = anchors[anchors.length - 1];
        if (!last || pair[1] >= last[1] + groups[last[0]]!.length) anchors.push(pair);
    }
    return anchors;
}

/** Each block at the next child after the previous match with the same content, for stretches no block is unique in. */
function matchInOrder(groups: readonly (readonly ProseMirrorNode[])[], children: readonly ProseMirrorNode[], blocks: Range, kids: Range, starts: number[]): void {
    const [firstBlock, lastBlock] = blocks;
    const [firstChild, lastChild] = kids;
    const positions = new Map<string, number[]>();
    for (let child = firstChild; child < lastChild; child++) {
        const key = keyOf(children[child]!);
        const list = positions.get(key);
        if (list) list.push(child);
        else positions.set(key, [child]);
    }
    const cursors = new Map<string, number>();
    let next = firstChild;
    for (let block = firstBlock; block < lastBlock; block++) {
        const group = groups[block]!;
        const key = keyOf(group[0]!);
        const list = positions.get(key) ?? [];
        let index = cursors.get(key) ?? 0;
        while (index < list.length && (list[index]! < next || !groupMatches(group, children, list[index]!, lastChild))) index += 1;
        cursors.set(key, index);
        if (index === list.length) continue;
        starts[block] = list[index]!;
        next = list[index]! + group.length;
        cursors.set(key, index + 1);
    }
}

const BLANK_LINE = /\n[ \t]*\n/;

/** The link reference definitions in a gap between blocks, without the blank lines around them. */
function definitionsOf(gap: string): string {
    return gap.replace(/^(?:[ \t]*\n)+/, "").replace(/\s+$/, "");
}

function trailingWhitespace(gap: string): string {
    return gap.match(/\s*$/)?.[0] ?? "";
}

interface GapContext {
    /** The text before the gap was newly written. */
    freshBefore: boolean;
    /** The text after the gap is newly written. */
    freshAfter: boolean;
    /** The gap starts or ends the document. */
    edge: boolean;
}

/** *gap* next to newly written text, which needs a blank line on that side to stay a block of its own. */
function separatedGap(gap: string, { freshBefore, freshAfter, edge }: GapContext): string {
    const definitions = definitionsOf(gap);
    if (!definitions) return edge || BLANK_LINE.test(gap) ? gap : "\n\n";
    const at = gap.indexOf(definitions);
    const leading = gap.slice(0, at);
    const trailing = gap.slice(at + definitions.length);
    return (freshBefore && !BLANK_LINE.test(leading) ? "\n\n" : leading) + definitions + (freshAfter && !BLANK_LINE.test(trailing) ? "\n\n" : trailing);
}

/**
 * The text between two unchanged blocks (or a block and a document edge): *written* is what was newly written there,
 * and *gaps* the original gaps that lay between them, one more than the original blocks the region replaced. Every
 * link reference definition in them is kept.
 */
export function joinRegion(gaps: readonly string[], written: string, { atStart, atEnd }: { atStart: boolean; atEnd: boolean }): string {
    const first = gaps[0] ?? "";
    const last = gaps[gaps.length - 1] ?? "";
    if (!written) {
        if (gaps.length === 1) return first;
        const definitions = gaps.map(definitionsOf).filter(Boolean);
        if (!definitions.length) return atStart ? first : atEnd ? last : separatedGap(last, { freshBefore: true, freshAfter: true, edge: false });
        return (atStart ? "" : "\n\n") + definitions.join("\n\n") + (atEnd ? trailingWhitespace(last) : "\n\n");
    }
    if (gaps.length === 1) {
        if (atStart && atEnd) {
            const definitions = definitionsOf(first);
            return written + (definitions ? `\n\n${definitions}` : "") + trailingWhitespace(first);
        }
        if (atEnd) return `\n\n${written}${separatedGap(first, { freshBefore: true, freshAfter: false, edge: true })}`;
        return `${separatedGap(first, { freshBefore: false, freshAfter: true, edge: atStart })}${written}\n\n`;
    }
    const inner = gaps
        .slice(1, -1)
        .map(definitionsOf)
        .filter(Boolean)
        .map((definitions) => `\n\n${definitions}`)
        .join("");
    return (
        separatedGap(first, { freshBefore: false, freshAfter: true, edge: atStart }) +
        written +
        inner +
        separatedGap(last, { freshBefore: true, freshAfter: false, edge: atEnd })
    );
}

/** Tracks the source an editor's document was loaded from, and writes the document back over it. */
export class ArticleSourceTracker {
    private baseline: Baseline = { blocks: [], gaps: [""] };

    constructor(private readonly editor: Editor) {}

    /** Replace the editor's document with *source*, without emitting an update or an undo step. */
    load(source: string): void {
        const { schema } = this.editor;
        const split = splitArticleSource(source);
        const parsed = this.parseBlocks(split);
        const blocks: BaselineBlock[] = split.blocks.map((block, index) => {
            const nodes = parsed[index]!;
            const editable = nodes.length > 0 && this.writesBack(block.text, nodes, split.env);
            return { text: block.text, nodes: editable ? nodes : [schema.nodes.markdownRawBlock!.create({ source: block.text })] };
        });
        const content = blocks.flatMap((block) => block.nodes);
        const { tr } = this.editor.state;
        tr.replaceWith(0, tr.doc.content.size, content.length ? content : schema.nodes.paragraph!.create());
        tr.setMeta("addToHistory", false).setMeta("preventUpdate", true);
        this.editor.view.dispatch(tr);
        this.baseline = { blocks, gaps: split.gaps };
    }

    /** The editor's document as Markdown, with every unchanged block and the text between blocks as loaded. */
    serialize(): string {
        const children: ProseMirrorNode[] = [];
        this.editor.state.doc.forEach((child) => children.push(child));
        // The editor keeps an empty paragraph after a trailing non-paragraph block; it writes nothing.
        while (children.length && children[children.length - 1]!.type.name === "paragraph" && children[children.length - 1]!.childCount === 0) children.pop();
        const { blocks, gaps } = this.baseline;
        const starts = alignBlocks(
            blocks.map((block) => block.nodes),
            children,
        );
        const blockAt = new Map<number, number>();
        starts.forEach((start, block) => {
            if (start >= 0) blockAt.set(start, block);
        });

        let out = "";
        let previous = -1;
        let fresh: ProseMirrorNode[] = [];
        let anyContent = false;
        const flush = (next: number): void => {
            const before = blocks[previous]?.text ?? null;
            const after = blocks[next]?.text ?? null;
            const written = this.writeRun(fresh, blocks.slice(previous + 1, next).map((block) => block.text), before, after);
            fresh = [];
            anyContent ||= written.length > 0 || next < blocks.length;
            out += joinRegion(gaps.slice(previous + 1, next + 1), written.join("\n\n"), { atStart: previous === -1, atEnd: next === blocks.length });
        };
        for (let index = 0; index < children.length; ) {
            const block = blockAt.get(index);
            if (block === undefined) {
                fresh.push(children[index]!);
                index += 1;
                continue;
            }
            flush(block);
            out += blocks[block]!.text;
            previous = block;
            index += blocks[block]!.nodes.length;
        }
        flush(blocks.length);
        return anyContent ? out : "";
    }

    /**
     * Markdown for a run of new or changed top-level nodes, one block each, between the unchanged blocks *before* and
     * *after*. A list keeps the marker of the original block it took the place of in *replaced*, and never shares a
     * marker with a list beside it, which would merge the two.
     */
    private writeRun(nodes: readonly ProseMirrorNode[], replaced: readonly string[], before: string | null, after: string | null): string[] {
        const written = nodes.map((node) => this.write([node])).filter(Boolean);
        if (!written.length) return written;
        written.forEach((text, index) => {
            const own = listMarkerOf(text);
            if (!own) return;
            const original = listMarkerOf(replaced[index] ?? "");
            const neighbours = [index === 0 ? before : written[index - 1], index === written.length - 1 ? after : written[index + 1]];
            const taken = neighbours.map((neighbour) => (neighbour ? listMarkerOf(neighbour) : null)).filter((marker) => marker?.kind === own.kind);
            const preferred = original?.kind === own.kind ? original.marker : own.marker;
            const marker = [preferred, ...LIST_MARKERS[own.kind]].find((candidate) => !taken.some((neighbour) => neighbour?.marker === candidate)) ?? preferred;
            if (marker !== own.marker) written[index] = withListMarker(text, marker);
        });
        const pieces = [before, ...written, after].filter((piece): piece is string => piece !== null);
        const expected = pieces.flatMap((piece) => splitArticleSource(piece).blocks.map((block) => block.text));
        const joined = splitArticleSource(pieces.join("\n\n")).blocks.map((block) => block.text);
        if (JSON.stringify(joined) !== JSON.stringify(expected)) {
            // Something still runs into its neighbour (a list absorbing an indented code block); a comment keeps them apart.
            return [...(before === null ? [] : [BLOCK_BREAK]), written.join(`\n\n${BLOCK_BREAK}\n\n`), ...(after === null ? [] : [BLOCK_BREAK])];
        }
        return written;
    }

    private write(nodes: readonly ProseMirrorNode[]): string {
        const { schema, storage } = this.editor;
        const markdown = storage.markdown.serializer.serialize(schema.topNodeType.create(null, nodes));
        return markdown.trim() ? markdown.replace(/\s+$/, "") : "";
    }

    /** Whether writing *nodes* back renders the same as *text*, the source they were parsed from. */
    private writesBack(text: string, nodes: readonly ProseMirrorNode[], env: ArticleEnv): boolean {
        try {
            const written = nodes.map((node) => this.write([node])).filter(Boolean);
            return comparableRendering(written.join("\n\n"), env) === comparableRendering(text, env);
        } catch {
            return false;
        }
    }

    /**
     * The nodes each block parses to ([] for raw blocks), parsed in one pass with a comment between blocks. Parsing a
     * block alone gives the same nodes; the single pass only saves time on long articles, and if the comments don't
     * come back one per gap, each block is parsed alone after all.
     */
    private parseBlocks(split: SplitSource): ProseMirrorNode[][] {
        const texts = split.blocks.filter((block) => !block.raw).map((block) => block.text);
        const segments: ProseMirrorNode[][] = [[]];
        if (texts.length) {
            for (const node of this.parse(texts.join(`\n\n${PARSE_BREAK}\n\n`), split.env)) {
                const isBreak = node.childCount === 1 && node.firstChild?.type.name === "markdownRawInline" && node.firstChild.attrs.source === PARSE_BREAK;
                if (isBreak) segments.push([]);
                else segments[segments.length - 1]!.push(node);
            }
        }
        const batched = segments.length === texts.length && segments.every((segment) => segment.length > 0);
        let next = 0;
        return split.blocks.map((block) => {
            if (block.raw) return [];
            const nodes = batched ? segments[next]! : this.parse(block.text, split.env);
            next += 1;
            return nodes;
        });
    }

    private parse(text: string, env: ArticleEnv): ProseMirrorNode[] {
        const html = withArticleEnv(env, () => this.editor.storage.markdown.parser.parse(text));
        const nodes: ProseMirrorNode[] = [];
        createDocument(html, this.editor.schema).forEach((node) => nodes.push(node));
        return nodes;
    }
}
