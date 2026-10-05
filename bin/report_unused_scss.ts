/**
 * Review unused and overridden SCSS. Writes a report; deletes nothing.
 *
 * Chromium already records the two facts this needs, so the script does not
 * reimplement the cascade:
 *
 * - `CSS.startRuleUsageTracking` marks a rule observed when its selector matched
 *   during style calculation. That is not proof it is unused: Django, HTMX, and
 *   script-built markup can be absent from this DOM.
 * - `CSS.getMatchedStylesForNode` returns the rules that matched a node, in
 *   cascade order. The walk below is the one DevTools uses: visit low to high,
 *   and an earlier `!important` beats a later normal declaration. Specificity,
 *   layers, and source order stay inside Chrome. Longhands are judged only
 *   when Chrome reports `longhandProperties` on the shorthand.
 *
 * Playwright's `chromium.launch` talks to Chrome over a debugging pipe that bun
 * on Windows never connects, so this script starts the Playwright-installed
 * Chromium itself and speaks the same CDP methods Playwright coverage uses.
 *
 * States (viewport, `[data-theme]`, `CSS.forcePseudoState`,
 * `Emulation.setEmulatedMedia`) are applied in that one session. PurgeCSS,
 * UnCSS, and Lighthouse are the wrong tool for the verdict: PurgeCSS deletes
 * from a token scan, UnCSS only runs load-time JavaScript and is unmaintained,
 * and Lighthouse reports unused bytes for a single navigation.
 *
 * The static pass is the token half of a PurgeCSS scan, plus duplicate
 * selectors and identical blocks. A missing token is "not a literal", not
 * "unused".
 *
 * Usage:
 *   bun run bin/report_unused_scss.ts --out unused-scss-report.md
 *   bun run bin/report_unused_scss.ts --max-nodes 250
 *   bun run bin/report_unused_scss.ts --static-only
 *
 * The default inspects 400 style signatures spread across the flattened
 * templates. There are far more signatures than that, so cascade findings are
 * candidates. Raise `--max-nodes` for a slower, wider sample. `--static-only`
 * skips Chromium and reports duplicate selectors, identical blocks, and
 * selectors whose classes and ids are not literals.
 */

import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import * as sass from "sass";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const SASS_ENTRY = join(ROOT, "src/urbanlens/dashboard/frontend/sass/style.scss");
const TEMPLATE_DIR = join(ROOT, "src/urbanlens/dashboard/templates");
const TS_DIR = join(ROOT, "src/urbanlens/dashboard/frontend/ts");
const TAG_DIR = join(ROOT, "src/urbanlens/dashboard/templatetags");

const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const SKIP_BLOCK = /^@(?:keyframes|-webkit-keyframes|font-face|page|counter-style|property|font-palette-values)\b/i;
const ENTER_BLOCK = /^@(?:media|supports|layer|container|scope)\b/i;
const HOVER_PSEUDOS = ["hover", "focus", "focus-visible", "active", "focus-within"];

export type SourcePoint = { file: string; line: number; column: number };

export type StyleDecl = {
    property: string;
    value: string;
    important: boolean;
    offset: number;
    source?: SourcePoint;
};

export type StyleRule = {
    selector: string;
    context: string;
    declarations: StyleDecl[];
    start: number;
    end: number;
    source?: SourcePoint;
};

export type Item = {
    file: string;
    line: number;
    selector: string;
    property: string;
    detail: string;
};

export type Report = {
    ruleCount: number;
    structuralCount: number;
    pages: number;
    unresolvedIncludes: number;
    inspectedNodes: number;
    representativeNodes: number;
    truncated: boolean;
    runtimeRan: boolean;
    runtimeError: string | null;
    states: string[];
    notObservedAbsent: Item[];
    notObservedPresent: Item[];
    notObservedOther: Item[];
    neverWon: Item[];
    partial: Item[];
    redundantAuthor: Item[];
    redundantUa: Item[];
    duplicateSelectors: Item[];
    identicalBlocks: Item[];
    unassessedDeclarations: number;
};

export type RuntimeState = {
    name: string;
    width: number;
    theme: "light" | "dark";
    pseudos: string[];
    features: { name: string; value: string }[];
    /** When false, the state only refreshes CSS coverage. */
    sample: boolean;
};

export const REVIEW_STATES: RuntimeState[] = [
    { name: "light 380", width: 380, theme: "light", pseudos: [], features: [], sample: true },
    { name: "light 760", width: 760, theme: "light", pseudos: [], features: [], sample: true },
    { name: "light 1280", width: 1280, theme: "light", pseudos: [], features: [], sample: true },
    { name: "dark 1280", width: 1280, theme: "dark", pseudos: [], features: [], sample: true },
    { name: "light 1280 hover", width: 1280, theme: "light", pseudos: HOVER_PSEUDOS, features: [], sample: true },
    { name: "dark 1280 hover", width: 1280, theme: "dark", pseudos: HOVER_PSEUDOS, features: [], sample: true },
    {
        name: "coarse pointer, reduced motion",
        width: 1280,
        theme: "light",
        pseudos: [],
        features: [
            { name: "prefers-reduced-motion", value: "reduce" },
            { name: "hover", value: "none" },
            { name: "pointer", value: "coarse" },
        ],
        sample: true,
    },
];

type Origin = "regular" | "user-agent" | "inline" | "injected";

export type AuthorDecl = {
    id: string;
    property: string;
    value: string;
    important: boolean;
    /** Cascade properties to judge. Longhands, when Chrome sent them; otherwise the property itself. */
    judged: string[];
    origin: Origin;
    selector: string;
    offset: number | null;
};

export type MatchedAuthorRule = {
    origin: Origin;
    selector: string;
    matchingSelectors: string[];
    declarations: AuthorDecl[];
};

export type CascadeHit = {
    group: string;
    property: string;
    authored: string;
    value: string;
    selector: string;
    offset: number | null;
    origin: Origin;
    outcome: "active" | "overridden";
    important: boolean;
    authorTwin: boolean;
    uaTwin: boolean;
};

type Tally = {
    group: string;
    property: string;
    authored: string;
    value: string;
    selector: string;
    offset: number | null;
    seen: number;
    active: number;
    authorTwin: number;
    uaTwin: number;
};

export type El = {
    nodeId: number;
    tag: string;
    attrs: Record<string, string>;
    children: El[];
};

type MappedPoint = {
    generatedLine: number;
    generatedColumn: number;
    sourceIndex: number;
    originalLine: number;
    originalColumn: number;
};

type SourceMap = { sources: string[]; mappings: string };

// --- CSS -----------------------------------------------------------------

function skipString(text: string, index: number): number {
    const quote = text[index];
    let i = index + 1;
    while (i < text.length) {
        if (text[i] === "\\") {
            i += 2;
            continue;
        }
        if (text[i] === quote) return i + 1;
        i++;
    }
    return text.length;
}

function matchingBrace(css: string, open: number): number {
    let depth = 0;
    let i = open;
    while (i < css.length) {
        if (css.startsWith("/*", i)) {
            const end = css.indexOf("*/", i + 2);
            i = end < 0 ? css.length : end + 2;
            continue;
        }
        const ch = css[i]!;
        if (ch === '"' || ch === "'") {
            i = skipString(css, i);
            continue;
        }
        if (ch === "{") depth++;
        else if (ch === "}") {
            depth--;
            if (depth === 0) return i;
        }
        i++;
    }
    return Math.max(open, css.length - 1);
}

function readPrelude(css: string, start: number): { start: number; end: number; closer: "{" | ";" | "eof"; text: string } {
    let i = start;
    let paren = 0;
    let bracket = 0;
    while (i < css.length) {
        if (css.startsWith("/*", i)) {
            const end = css.indexOf("*/", i + 2);
            i = end < 0 ? css.length : end + 2;
            continue;
        }
        const ch = css[i]!;
        if (ch === '"' || ch === "'") {
            i = skipString(css, i);
            continue;
        }
        if (ch === "(") paren++;
        else if (ch === ")" && paren) paren--;
        else if (ch === "[") bracket++;
        else if (ch === "]" && bracket) bracket--;
        else if (ch === "{" && paren === 0 && bracket === 0) {
            return { start, end: i, closer: "{", text: css.slice(start, i).trim() };
        } else if (ch === ";" && paren === 0 && bracket === 0) {
            return { start, end: i, closer: ";", text: css.slice(start, i).trim() };
        }
        i++;
    }
    return { start, end: css.length, closer: "eof", text: css.slice(start).trim() };
}

function parseDecls(body: string, base: number): StyleDecl[] {
    const decls: StyleDecl[] = [];
    let paren = 0;
    let start = 0;
    const parts: { text: string; at: number }[] = [];
    for (let i = 0; i < body.length; i++) {
        if (body.startsWith("/*", i)) {
            const end = body.indexOf("*/", i + 2);
            i = end < 0 ? body.length : end + 1;
            continue;
        }
        const ch = body[i]!;
        if (ch === '"' || ch === "'") {
            i = skipString(body, i) - 1;
            continue;
        }
        if (ch === "(") paren++;
        else if (ch === ")" && paren) paren--;
        else if (ch === ";" && paren === 0) {
            parts.push({ text: body.slice(start, i), at: base + start });
            start = i + 1;
        }
    }
    const tail = body.slice(start);
    if (tail.trim()) parts.push({ text: tail, at: base + start });
    for (const part of parts) {
        const text = part.text.replace(/\/\*[\s\S]*?\*\//g, "").trim();
        if (!text || text.startsWith("@")) continue;
        const colon = text.indexOf(":");
        if (colon <= 0) continue;
        let value = text.slice(colon + 1).trim();
        const important = /!important\s*$/i.test(value);
        if (important) value = value.replace(/!important\s*$/i, "").trim();
        const property = text.slice(0, colon).trim().toLowerCase();
        if (!property) continue;
        decls.push({ property, value, important, offset: part.at });
    }
    return decls;
}

/** Style rules in source order. `@media` / `@supports` / `@layer` stay as context. Keyframes and font faces are counted, not judged. */
export function parseStyleRules(css: string): { rules: StyleRule[]; structural: number } {
    const rules: StyleRule[] = [];
    const context: string[] = [];
    let structural = 0;
    let i = 0;
    while (i < css.length) {
        while (i < css.length) {
            if (css.startsWith("/*", i)) {
                const end = css.indexOf("*/", i + 2);
                i = end < 0 ? css.length : end + 2;
                continue;
            }
            if (/\s/.test(css[i]!)) {
                i++;
                continue;
            }
            break;
        }
        if (i >= css.length) break;
        if (css[i] === "}") {
            context.pop();
            i++;
            continue;
        }
        const prelude = readPrelude(css, i);
        if (prelude.closer === "eof") break;
        if (prelude.closer === ";") {
            i = prelude.end + 1;
            continue;
        }
        const close = matchingBrace(css, prelude.end);
        const header = prelude.text.replace(/\/\*[\s\S]*?\*\//g, "").trim();
        if (header.startsWith("@")) {
            if (SKIP_BLOCK.test(header) || !ENTER_BLOCK.test(header)) structural++;
            else context.push(normalizeSpace(header));
            i = header.startsWith("@") && ENTER_BLOCK.test(header) ? prelude.end + 1 : close + 1;
            continue;
        }
        if (header) {
            rules.push({
                selector: header,
                context: context.join("\n"),
                declarations: parseDecls(css.slice(prelude.end + 1, close), prelude.end + 1),
                start: prelude.start,
                end: close + 1,
            });
        }
        i = close + 1;
    }
    return { rules, structural };
}

export function normalizeSpace(value: string): string {
    return value.replace(/\s+/g, " ").trim();
}

export function normalizeSelector(selector: string): string {
    return normalizeSpace(selector).replace(/\s*([>+~])\s*/g, " $1 ");
}

export function selectorIdentifiers(selector: string): { classes: string[]; ids: string[] } {
    const stripped = selector.replace(/\[[^\]]*\]/g, "");
    const classes = [...stripped.matchAll(/\.((?:\\.|[\w-])+)/g)].map((match) => match[1]!.replaceAll("\\", ""));
    const ids = [...stripped.matchAll(/#((?:\\.|[\w-])+)/g)].map((match) => match[1]!.replaceAll("\\", ""));
    return { classes, ids };
}

function ruleKey(rule: StyleRule): string {
    return `${normalizeSelector(rule.selector)}@@${normalizeSpace(rule.context)}`;
}

// --- Source maps ---------------------------------------------------------

function decodeVlq(segment: string): number[] {
    const values: number[] = [];
    let current = 0;
    let shift = 0;
    for (const char of segment) {
        const digit = B64.indexOf(char);
        if (digit < 0) continue;
        current += (digit & 31) << shift;
        if (digit & 32) {
            shift += 5;
            continue;
        }
        values.push(current & 1 ? -(current >> 1) : current >> 1);
        current = 0;
        shift = 0;
    }
    return values;
}

export function decodeMappings(mappings: string): MappedPoint[] {
    const points: MappedPoint[] = [];
    let sourceIndex = 0;
    let originalLine = 0;
    let originalColumn = 0;
    const lines = mappings.split(";");
    for (let line = 0; line < lines.length; line++) {
        let column = 0;
        for (const segment of lines[line]!.split(",")) {
            if (!segment) continue;
            const fields = decodeVlq(segment);
            if (!fields.length) continue;
            column += fields[0] ?? 0;
            if (fields.length >= 4) {
                sourceIndex += fields[1] ?? 0;
                originalLine += fields[2] ?? 0;
                originalColumn += fields[3] ?? 0;
                points.push({ generatedLine: line, generatedColumn: column, sourceIndex, originalLine, originalColumn });
            }
        }
    }
    return points;
}

function lineStartsOf(css: string): number[] {
    const starts = [0];
    for (let i = 0; i < css.length; i++) if (css[i] === "\n") starts.push(i + 1);
    return starts;
}

function lineCol(starts: number[], offset: number): { line: number; column: number } {
    let lo = 0;
    let hi = starts.length - 1;
    while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const start = starts[mid] ?? 0;
        const next = starts[mid + 1] ?? Number.POSITIVE_INFINITY;
        if (offset < start) hi = mid - 1;
        else if (offset >= next) lo = mid + 1;
        else return { line: mid, column: offset - start };
    }
    return { line: 0, column: 0 };
}

function pointAt(points: MappedPoint[], line: number, column: number): MappedPoint | null {
    let lo = 0;
    let hi = points.length - 1;
    let best: MappedPoint | null = null;
    while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const point = points[mid]!;
        const before = point.generatedLine < line || (point.generatedLine === line && point.generatedColumn <= column);
        if (before) {
            best = point;
            lo = mid + 1;
        } else hi = mid - 1;
    }
    return best;
}

function sourceLabel(source: string): string {
    const url = source.startsWith("file:") ? fileURLToPath(source) : source;
    const rel = relative(ROOT, url);
    return rel.startsWith("..") ? url : rel.replaceAll("\\", "/");
}

type PreparedMap = { starts: number[]; points: MappedPoint[]; sources: readonly string[] };

function prepareMap(css: string, map: SourceMap | undefined): PreparedMap | undefined {
    if (!map?.mappings) return undefined;
    return { starts: lineStartsOf(css), points: decodeMappings(map.mappings), sources: map.sources };
}

function locatePrepared(prepared: PreparedMap, offset: number): SourcePoint | undefined {
    const { line, column } = lineCol(prepared.starts, offset);
    const point = pointAt(prepared.points, line, column);
    if (!point) return undefined;
    const file = prepared.sources[point.sourceIndex];
    if (!file) return undefined;
    return { file: sourceLabel(file), line: point.originalLine + 1, column: point.originalColumn + 1 };
}

export function locate(css: string, map: SourceMap | undefined, offset: number): SourcePoint | undefined {
    const prepared = prepareMap(css, map);
    return prepared ? locatePrepared(prepared, offset) : undefined;
}

function attachSources(rules: StyleRule[], css: string, map: SourceMap | undefined): void {
    const prepared = prepareMap(css, map);
    if (!prepared) return;
    for (const rule of rules) {
        const source = locatePrepared(prepared, rule.start);
        if (source) rule.source = source;
        for (const decl of rule.declarations) {
            let offset = decl.offset;
            while (offset < css.length && /\s/.test(css[offset] ?? "")) offset++;
            const at = locatePrepared(prepared, offset);
            if (at) decl.source = at;
        }
    }
}

// --- Templates -----------------------------------------------------------

function stripComments(src: string): string {
    return src.replace(/\{#[\s\S]*?#\}/g, "").replace(/\{%\s*comment\s*%\}[\s\S]*?\{%\s*endcomment\s*%\}/g, "");
}

function escapeRegExp(value: string): string {
    return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function prepare(src: string, inclusions: Map<string, string>): string {
    let text = stripComments(src).replace(/\{%\s*verbatim\s*%\}([\s\S]*?)\{%\s*endverbatim\s*%\}/g, "$1");
    if (!inclusions.size) return text;
    const names = [...inclusions.keys()].sort((a, b) => b.length - a.length).map(escapeRegExp).join("|");
    text = text.replace(new RegExp(String.raw`\{%\s*(?:${names})\b[^%]*%\}`, "g"), (full) => {
        const name = /\{%\s*([A-Za-z0-9_]+)/.exec(full)?.[1];
        const path = name ? inclusions.get(name) : undefined;
        return path ? `{% include "${path}" %}` : full;
    });
    return text;
}

/** Innermost `{% block %}` first, so a child can replace `b` inside an untouched `a`. */
export function extractBlocks(src: string): { skeleton: string; blocks: Map<string, string> } {
    const blocks = new Map<string, string>();
    let cur = src;
    const inner = /\{%\s*block\s+([A-Za-z0-9_]+)\s*%\}((?:(?!\{%\s*(?:block|endblock)\b)[\s\S])*?)\{%\s*endblock(?:\s+[A-Za-z0-9_]+)?\s*%\}/;
    for (let guard = 0; guard < 500; guard++) {
        const match = inner.exec(cur);
        if (!match) break;
        blocks.set(match[1]!, match[2] ?? "");
        cur = `${cur.slice(0, match.index)}%%BLOCK:${match[1]}%%${cur.slice(match.index + match[0].length)}`;
    }
    return { skeleton: cur, blocks };
}

function substitute(html: string, blocks: Map<string, string>): string {
    let cur = html;
    for (let guard = 0; guard < 50; guard++) {
        const next = cur.replace(/%%BLOCK:([A-Za-z0-9_]+)%%/g, (_, name: string) => blocks.get(name) ?? "");
        if (next === cur) break;
        cur = next;
    }
    return cur;
}

function extendsPath(src: string): string | null {
    return /\{%\s*extends\s+(['"])([^'"]+)\1/.exec(src)?.[2]?.replaceAll("\\", "/") ?? null;
}

function extendChain(leaf: string, files: Map<string, string>): string[] {
    const chain = [leaf];
    const seen = new Set([leaf]);
    let name = leaf;
    for (;;) {
        const ext = extendsPath(files.get(name) ?? "");
        if (!ext || seen.has(ext) || !files.has(ext)) break;
        seen.add(ext);
        chain.push(ext);
        name = ext;
    }
    return chain.reverse();
}

function stripTags(html: string): string {
    return html.replace(/\{%[\s\S]*?%\}/g, "").replace(/\{\{[\s\S]*?\}\}/g, "");
}

function renderPage(name: string, files: Map<string, string>, stack: string[], stats: { unresolved: number }): string {
    if (stack.includes(name) || stack.length > 40) return "";
    if (!files.has(name)) {
        stats.unresolved++;
        return "";
    }
    const chain = extendChain(name, files);
    const root = extractBlocks(files.get(chain[0]!) ?? "");
    const merged = new Map(root.blocks);
    for (const item of chain.slice(1)) {
        for (const [key, value] of extractBlocks(files.get(item) ?? "").blocks) merged.set(key, value);
    }
    return stripTags(expandIncludes(substitute(root.skeleton, merged), files, [...stack, name], stats));
}

function expandIncludes(html: string, files: Map<string, string>, stack: string[], stats: { unresolved: number }): string {
    let cur = html;
    const quoted = /\{%\s*include\s+(['"])([^'"]+)\1[^%]*%\}/;
    for (let guard = 0; guard < 5000; guard++) {
        const match = quoted.exec(cur);
        if (!match) break;
        const key = match[2]!.replaceAll("\\", "/");
        const replacement = stack.includes(key) ? "" : renderPage(key, files, stack, stats);
        cur = `${cur.slice(0, match.index)}${replacement}${cur.slice(match.index + match[0].length)}`;
    }
    return cur.replace(/\{%\s*include\s+(?!['"])[^%]*%\}/g, () => {
        stats.unresolved++;
        return "";
    });
}

/** ``html`` with every script block removed, repeated until none is left, closing tags in any spacing included. */
function withoutScripts(html: string): string {
    let previous: string;
    let current = html;
    do {
        previous = current;
        current = current.replace(/<script\b[^>]*>[\s\S]*?<\/script\b[^>]*>/gi, "");
    } while (current !== previous);
    return current;
}

function toFragment(html: string): { className: string; html: string } {
    const className = /<body\b[^>]*\bclass\s*=\s*(['"])([\s\S]*?)\1/i.exec(html)?.[2] ?? "";
    const fragment = withoutScripts(html)
        .replace(/<!DOCTYPE[^>]*>/gi, "")
        .replace(/<head\b[^>]*>[\s\S]*?<\/head>/gi, "")
        .replace(/<\/?(?:html|body)\b[^>]*>/gi, "")
        .replace(/<link\b[^>]*>/gi, "")
        .replace(/<meta\b[^>]*>/gi, "");
    return { className: normalizeSpace(className), html: fragment };
}

function escapeAttr(value: string): string {
    return value.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
}

/**
 * One fragment per page that `{% extends %}`, plus templates nothing includes.
 * `{% if %}` / `{% else %}` both stay, so a selector used on only one branch still matches.
 * Variable `{% include %}` is counted and dropped.
 */
export function renderTemplates(files: Map<string, string>, inclusions: Map<string, string>): { html: string; pages: number; unresolved: number } {
    const prepared = new Map<string, string>();
    for (const [name, src] of files) prepared.set(name, prepare(src, inclusions));
    const referenced = new Set<string>(inclusions.values());
    for (const src of files.values()) {
        for (const match of src.matchAll(/\{%\s*include\s+(['"])([^'"]+)\1/g)) referenced.add(match[2]!.replaceAll("\\", "/"));
        for (const match of src.matchAll(/\{%\s*extends\s+(['"])([^'"]+)\1/g)) referenced.add(match[2]!.replaceAll("\\", "/"));
    }
    const stats = { unresolved: 0 };
    const sections: string[] = [];
    for (const name of [...prepared.keys()].sort()) {
        const page = /\{%\s*extends\b/.test(prepared.get(name) ?? "");
        if (!page && referenced.has(name)) continue;
        const rendered = toFragment(renderPage(name, prepared, [], stats));
        sections.push(`<div data-ul-page="${escapeAttr(name)}" class="${escapeAttr(rendered.className)}">${rendered.html}</div>`);
    }
    return { html: sections.join("\n"), pages: sections.length, unresolved: stats.unresolved };
}

export function collectTokens(texts: Iterable<string>): { classes: Set<string>; ids: Set<string> } {
    const classes = new Set<string>();
    const ids = new Set<string>();
    const literal = /["'`]([A-Za-z_][\w-]{0,80})["'`]/g;
    for (const text of texts) {
        for (const match of text.matchAll(/\bclass\s*=\s*(['"])([\s\S]*?)\1/gi)) {
            for (const token of match[2]!.split(/\s+/)) if (/^[A-Za-z_][\w-]*$/.test(token)) classes.add(token);
        }
        for (const match of text.matchAll(/\bid\s*=\s*(['"])([\s\S]*?)\1/gi)) {
            const id = normalizeSpace(match[2] ?? "");
            if (/^[A-Za-z_][\w-]*$/.test(id)) ids.add(id);
        }
        for (const match of text.matchAll(literal)) {
            classes.add(match[1]!);
            ids.add(match[1]!);
        }
    }
    return { classes, ids };
}

function identifierStatus(selector: string, tokens: { classes: Set<string>; ids: Set<string> }): { kind: "absent" | "present" | "none"; missing: string[] } {
    const { classes, ids } = selectorIdentifiers(selector);
    if (!classes.length && !ids.length) return { kind: "none", missing: [] };
    const missing = [...classes.filter((name) => !tokens.classes.has(name)).map((name) => `.${name}`), ...ids.filter((name) => !tokens.ids.has(name)).map((name) => `#${name}`)];
    const any = classes.some((name) => tokens.classes.has(name)) || ids.some((name) => tokens.ids.has(name));
    return { kind: any ? "present" : "absent", missing };
}

// --- Cascade -------------------------------------------------------------

function sameValue(a: string, b: string): boolean {
    const norm = (value: string) => normalizeSpace(value).toLowerCase();
    if (norm(a) === norm(b)) return true;
    const zero = /^0(?:px|em|rem|%|pt)?$/;
    return zero.test(norm(a)) && zero.test(norm(b));
}

/**
 * `rules` must be Chrome's `matchedCSSRules` order (lowest priority first),
 * with inline style appended last. An earlier `!important` beats a later
 * normal declaration; everything else yields to whatever Chrome ordered later.
 */
export function cascadeFromMatches(rules: MatchedAuthorRule[]): CascadeHit[] {
    const hits: CascadeHit[] = [];
    const activeIndex = new Map<string, number>();
    for (const rule of rules) {
        for (const decl of rule.declarations) {
            for (const property of decl.judged) {
                const prevIndex = activeIndex.get(property);
                const prev = prevIndex === undefined ? undefined : hits[prevIndex];
                if (prev?.important && !decl.important) {
                    hits.push({
                        group: decl.id,
                        property,
                        authored: decl.property,
                        value: decl.value,
                        selector: decl.selector,
                        offset: decl.offset,
                        origin: decl.origin,
                        outcome: "overridden",
                        important: decl.important,
                        authorTwin: false,
                        uaTwin: false,
                    });
                    continue;
                }
                if (prev) prev.outcome = "overridden";
                const hit: CascadeHit = {
                    group: decl.id,
                    property,
                    authored: decl.property,
                    value: decl.value,
                    selector: decl.selector,
                    offset: decl.offset,
                    origin: decl.origin,
                    outcome: "active",
                    important: decl.important,
                    authorTwin: Boolean(prev && prev.origin === "regular" && decl.origin === "regular" && sameValue(prev.value, decl.value)),
                    uaTwin: Boolean(prev && prev.origin === "user-agent" && decl.origin === "regular" && sameValue(prev.value, decl.value)),
                    };
                activeIndex.set(property, hits.length);
                hits.push(hit);
            }
        }
    }
    return hits;
}

// --- Duplicates ----------------------------------------------------------

function formatWhere(rule: StyleRule): string {
    return rule.source ? `${rule.source.file}:${rule.source.line}` : normalizeSelector(rule.selector);
}

function duplicateSelectorFindings(rules: StyleRule[]): Item[] {
    const groups = new Map<string, StyleRule[]>();
    for (const rule of rules) {
        const key = ruleKey(rule);
        groups.set(key, [...(groups.get(key) ?? []), rule]);
    }
    const items: Item[] = [];
    for (const list of groups.values()) {
        if (list.length < 2) continue;
        const first = list[0]!;
        items.push({
            file: first.source?.file ?? "",
            line: first.source?.line ?? 0,
            selector: normalizeSelector(first.selector),
            property: "",
            detail: `compiled ${list.length} times in the same context: ${list.map(formatWhere).join(", ")}`,
        });
    }
    return items;
}

function identicalBlockFindings(rules: StyleRule[]): Item[] {
    const groups = new Map<string, StyleRule[]>();
    for (const rule of rules) {
        if (rule.declarations.length < 3) continue;
        const block = rule.declarations.map((decl) => `${decl.property}:${normalizeSpace(decl.value)}${decl.important ? "!important" : ""}`).join(";");
        const key = `${normalizeSpace(rule.context)}@@${block}`;
        groups.set(key, [...(groups.get(key) ?? []), rule]);
    }
    const items: Item[] = [];
    for (const list of groups.values()) {
        const selectors = [...new Set(list.map((rule) => normalizeSelector(rule.selector)))];
        if (selectors.length < 2) continue;
        const first = list[0]!;
        items.push({
            file: first.source?.file ?? "",
            line: first.source?.line ?? 0,
            selector: selectors.join(", "),
            property: "",
            detail: `${first.declarations.length} identical declarations at ${list.map(formatWhere).join(", ")}`,
        });
    }
    return items;
}

// --- Representatives -----------------------------------------------------

function classSignature(attrs: Record<string, string>): string {
    return (attrs["class"] ?? "").split(/\s+/).filter(Boolean).sort().join(".");
}

const STYLE_ATTRS = ["aria-checked", "aria-current", "aria-expanded", "aria-pressed", "aria-selected", "checked", "data-state", "data-theme", "disabled", "hidden", "open", "readonly", "role", "selected", "style", "type"];

function keptAttrs(attrs: Record<string, string>): string {
    return STYLE_ATTRS.filter((key) => attrs[key] !== undefined)
        .map((key) => `${key}=${(attrs[key] ?? "").slice(0, 40)}`)
        .join("|");
}

/**
 * One node per style-relevant signature. Page wrappers (`data-ul-page`) are
 * transparent so repeated chrome collapses. Sibling index stops at 3, so a
 * long list of the same row is four samples, not one per row. The id value is
 * not part of the signature: coverage still sees every id, and an id-only rule
 * that was not the sampled node stays unassessed rather than "never won".
 */
export function pickRepresentatives(roots: El[]): number[] {
    const seen = new Set<string>();
    const ids: number[] = [];
    const walk = (node: El, parentSig: string, index: number): void => {
        if (node.attrs["data-ul-page"] !== undefined) {
            node.children.forEach((child, childIndex) => walk(child, parentSig, childIndex));
            return;
        }
        const sibling = Math.min(index, 3);
        const sig = `${parentSig}/${sibling}:${node.tag}#${node.attrs["id"] ? "1" : ""}.${classSignature(node.attrs)}|${keptAttrs(node.attrs)}`;
        if (!seen.has(sig)) {
            seen.add(sig);
            ids.push(node.nodeId);
        }
        node.children.forEach((child, childIndex) => walk(child, sig, childIndex));
    };
    roots.forEach((node, index) => walk(node, "", index));
    return ids;
}

/** Spread a cap across the signature list so the sample is not only the first templates. */
function sampleIds(ids: number[], max: number): number[] {
    if (ids.length <= max) return ids;
    const picked: number[] = [];
    const step = ids.length / max;
    for (let i = 0; i < max; i++) {
        const id = ids[Math.floor(i * step)];
        if (id !== undefined) picked.push(id);
    }
    return picked;
}

function elementsFromDom(node: { nodeId?: number; nodeType?: number; nodeName?: string; attributes?: string[]; children?: unknown[] }): El | null {
    if (node.nodeType !== 1 || node.nodeId === undefined) {
        const children = (node.children ?? []).map((child) => elementsFromDom(child as typeof node)).filter((child): child is El => child !== null);
        if (!children.length) return null;
        return { nodeId: node.nodeId ?? -1, tag: "fragment", attrs: { "data-ul-page": "" }, children };
    }
    const attrs: Record<string, string> = {};
    const flat = node.attributes ?? [];
    for (let i = 0; i < flat.length; i += 2) {
        const key = flat[i];
        if (key) attrs[key] = flat[i + 1] ?? "";
    }
    const tag = (node.nodeName ?? "").toLowerCase();
    if (tag === "script" || tag === "link" || tag === "meta" || tag === "style") return null;
    const children = (node.children ?? []).map((child) => elementsFromDom(child as typeof node)).filter((child): child is El => child !== null);
    return { nodeId: node.nodeId, tag, attrs, children };
}

// --- Runtime -------------------------------------------------------------

type CssRange = { startLine?: number; startColumn?: number };
type CssProperty = { name?: string; value?: string; important?: boolean; implicit?: boolean; disabled?: boolean; range?: CssRange; longhandProperties?: { name?: string }[] };
type CssRule = { origin?: string; selectorList?: { text?: string; selectors?: { text?: string }[] }; style?: { cssProperties?: CssProperty[]; range?: CssRange } };
type RuleMatch = { rule?: CssRule; matchingSelectors?: number[] };
type MatchedPayload = { matchedCSSRules?: RuleMatch[]; inlineStyle?: { cssProperties?: CssProperty[] }; pseudoElements?: { matches?: RuleMatch[] }[] };

function originOf(value: string | undefined): Origin {
    if (value === "user-agent" || value === "injected" || value === "regular") return value;
    return "regular";
}

function declsFromStyle(style: { cssProperties?: CssProperty[] } | undefined, selector: string, origin: Origin, css: string | null, lineStarts: number[] | null): AuthorDecl[] {
    const decls: AuthorDecl[] = [];
    for (const prop of style?.cssProperties ?? []) {
        if (!prop.name || prop.disabled || prop.implicit || prop.value === undefined) continue;
        if (style && prop.range === undefined && (style.cssProperties?.some((item) => item.range) ?? false)) continue;
        const property = prop.name.toLowerCase();
        const longhands = (prop.longhandProperties ?? []).map((item) => item.name?.toLowerCase()).filter((name): name is string => Boolean(name));
        let offset: number | null = null;
        if (css && lineStarts && prop.range?.startLine !== undefined && prop.range.startColumn !== undefined) {
            offset = (lineStarts[prop.range.startLine] ?? 0) + prop.range.startColumn;
        }
        decls.push({
            id: offset === null ? `sel:${normalizeSelector(selector)}|${property}|${prop.value}|${prop.range?.startLine ?? 0}:${prop.range?.startColumn ?? decls.length}` : `off:${offset}`,
            property,
            value: prop.value,
            important: Boolean(prop.important),
            judged: longhands.length ? longhands : [property],
            origin,
            selector,
            offset,
        });
    }
    return decls;
}

function rulesFromMatches(matches: RuleMatch[] | undefined, css: string | null, lineStarts: number[] | null): MatchedAuthorRule[] {
    const rules: MatchedAuthorRule[] = [];
    for (const match of matches ?? []) {
        const rule = match.rule;
        if (!rule?.style) continue;
        const origin = originOf(rule.origin);
        const selectors = (rule.selectorList?.selectors ?? []).map((selector) => selector.text ?? "").filter(Boolean);
        const matching = (match.matchingSelectors ?? []).map((index) => selectors[index] ?? "").filter(Boolean);
        rules.push({
            origin,
            selector: rule.selectorList?.text ?? "",
            matchingSelectors: matching,
            declarations: declsFromStyle(rule.style, rule.selectorList?.text ?? "", origin, css, lineStarts),
        });
    }
    return rules;
}

async function pool(count: number, limit: number, fn: (index: number) => Promise<void>): Promise<void> {
    let cursor = 0;
    const workers = Array.from({ length: Math.min(limit, count) }, async () => {
        for (;;) {
            const index = cursor++;
            if (index >= count) return;
            await fn(index);
        }
    });
    await Promise.all(workers);
}

class CdpSocket {
    #id = 0;
    #pending = new Map<number, { resolve: (value: unknown) => void; reject: (error: Error) => void }>();

    constructor(private readonly ws: WebSocket) {
        ws.addEventListener("message", (event) => {
            const message = JSON.parse(String(event.data)) as { id?: number; error?: { message: string }; result?: unknown };
            if (!message.id) return;
            const pending = this.#pending.get(message.id);
            if (!pending) return;
            this.#pending.delete(message.id);
            if (message.error) pending.reject(new Error(message.error.message));
            else pending.resolve(message.result);
        });
    }

    send(method: string, params: object = {}, sessionId?: string): Promise<unknown> {
        const id = ++this.#id;
        return new Promise((resolve, reject) => {
            this.#pending.set(id, { resolve, reject });
            this.ws.send(JSON.stringify({ id, method, params, sessionId }));
        });
    }
}

function findChrome(): string {
    const root = join(process.env["LOCALAPPDATA"] ?? "", "ms-playwright");
    if (existsSync(root)) {
        const versions = readdirSync(root).filter((name) => /^chromium-\d+$/.test(name)).sort();
        const newest = versions.at(-1);
        if (newest) {
            const exe = join(root, newest, "chrome-win64", "chrome.exe");
            if (existsSync(exe)) return exe;
        }
    }
    for (const exe of [join(process.env["PROGRAMFILES"] ?? "", "Google", "Chrome", "Application", "chrome.exe"), join(process.env["PROGRAMFILES(X86)"] ?? "", "Google", "Chrome", "Application", "chrome.exe")]) {
        if (existsSync(exe)) return exe;
    }
    throw new Error("Chromium was not found. Run `bunx playwright install chromium`.");
}

async function withChrome<T>(fn: (send: (method: string, params?: object) => Promise<unknown>) => Promise<T>): Promise<T> {
    const dir = mkdtempSync(join(tmpdir(), "ul-scss-chrome-"));
    const proc = spawn(findChrome(), ["--headless=new", "--remote-debugging-port=0", `--user-data-dir=${dir}`, "--no-first-run", "--disable-gpu", "--disable-extensions"], { stdio: "ignore" });
    const portFile = join(dir, "DevToolsActivePort");
    try {
        let port = "";
        let socketPath = "";
        for (let attempt = 0; attempt < 80; attempt++) {
            if (existsSync(portFile)) {
                const parts = readFileSync(portFile, "utf8").trim().split(/\r?\n/);
                if (parts[0] && parts[1]?.startsWith("/")) {
                    port = parts[0];
                    socketPath = parts[1];
                    break;
                }
            }
            await new Promise((resolve) => setTimeout(resolve, 50));
        }
        if (!port || !socketPath) throw new Error("Chromium did not open a debugging port.");
        const ws = new WebSocket(`ws://127.0.0.1:${port}${socketPath}`);
        ws.onerror = () => {};
        await new Promise<void>((resolve, reject) => {
            ws.addEventListener("open", () => resolve(), { once: true });
            ws.addEventListener("error", () => reject(new Error("Could not connect to Chromium.")), { once: true });
        });
        const cdp = new CdpSocket(ws);
        try {
            const created = (await cdp.send("Target.createTarget", { url: "about:blank" })) as { targetId: string };
            const attached = (await cdp.send("Target.attachToTarget", { targetId: created.targetId, flatten: true })) as { sessionId: string };
            const sessionId = attached.sessionId;
            return await fn((method, params = {}) => cdp.send(method, params, sessionId));
        } finally {
            ws.close();
        }
    } finally {
        if (proc.pid) spawnSync("taskkill", ["/pid", String(proc.pid), "/t", "/f"], { stdio: "ignore" });
        else proc.kill();
        try {
            rmSync(dir, { recursive: true, force: true });
        } catch {
            // The profile directory can still be locked for a moment after Chrome exits.
        }
    }
}

export async function runRuntime(options: {
    css: string;
    html: string;
    states: RuntimeState[];
    maxNodes: number;
    onProgress?: (message: string) => void;
}): Promise<{ observedKeys: Set<string> | null; tallies: Map<string, Tally>; inspected: number; representatives: number; truncated: boolean; textMatched: boolean }> {
    return withChrome(async (send) => {
        const safeCss = options.css.replace(/<\/style/gi, "<\\/style");
        const html = `<!DOCTYPE html><html id="html-root"><head><style>${safeCss}</style></head><body>${options.html}</body></html>`;
        await send("Page.enable");
        await send("DOM.enable");
        await send("CSS.enable");
        await send("CSS.startRuleUsageTracking");
        const tree = (await send("Page.getFrameTree")) as { frameTree: { frame: { id: string } } };
        await send("Page.setDocumentContent", { frameId: tree.frameTree.frame.id, html });
        const document = (await send("DOM.getDocument", { depth: -1, pierce: false })) as { root?: unknown };
        const root = document.root ? elementsFromDom(document.root as Parameters<typeof elementsFromDom>[0]) : null;
        const representatives = root ? pickRepresentatives([root]) : [];
        const nodeIds = sampleIds(representatives, options.maxNodes);
        const truncated = representatives.length > nodeIds.length;
        const tallies = new Map<string, Tally>();
        const lineStarts = lineStartsOf(options.css);
        let pseudosDirty = false;
        const log = options.onProgress ?? (() => {});
        log(`${representatives.length} style signatures; inspecting ${nodeIds.length}`);

        for (const state of options.states) {
            log(state.sample ? `sampling ${state.name} (${nodeIds.length} nodes)` : `coverage ${state.name}`);
            await send("Emulation.setDeviceMetricsOverride", { width: state.width, height: 900, deviceScaleFactor: 1, mobile: false });
            await send("Runtime.evaluate", {
                expression: state.theme === "dark" ? "document.documentElement.setAttribute('data-theme','dark')" : "document.documentElement.removeAttribute('data-theme')",
            });
            await send("Emulation.setEmulatedMedia", { media: "screen", features: state.features });
            if (nodeIds.length && (state.pseudos.length || pseudosDirty)) {
                await pool(nodeIds.length, 24, async (index) => {
                    const nodeId = nodeIds[index];
                    if (nodeId === undefined) return;
                    await send("CSS.forcePseudoState", { nodeId, forcedPseudoClasses: state.pseudos });
                });
                pseudosDirty = state.pseudos.length > 0;
            }
            await send("Runtime.evaluate", { expression: "document.body && document.body.getBoundingClientRect()" });
            if (!state.sample) continue;
            await pool(nodeIds.length, 24, async (index) => {
                const nodeId = nodeIds[index];
                if (nodeId === undefined) return;
                let payload: MatchedPayload;
                try {
                    payload = (await send("CSS.getMatchedStylesForNode", { nodeId })) as MatchedPayload;
                } catch {
                    return;
                }
                const walks = [rulesFromMatches(payload.matchedCSSRules, options.css, lineStarts)];
                if (payload.inlineStyle?.cssProperties?.length) {
                    walks[0]!.push({
                        origin: "inline",
                        selector: "[style]",
                        matchingSelectors: [],
                        declarations: declsFromStyle(payload.inlineStyle, "[style]", "inline", null, null),
                    });
                }
                for (const pseudo of payload.pseudoElements ?? []) walks.push(rulesFromMatches(pseudo.matches, options.css, lineStarts));
                for (const matched of walks) {
                    for (const hit of cascadeFromMatches(matched)) {
                        if (hit.origin !== "regular") continue;
                        const key = `${hit.group}@${hit.property}`;
                        const tally = tallies.get(key) ?? {
                            group: hit.group,
                            property: hit.property,
                            authored: hit.authored,
                            value: hit.value,
                            selector: hit.selector,
                            offset: hit.offset,
                            seen: 0,
                            active: 0,
                            authorTwin: 0,
                            uaTwin: 0,
                        };
                        tally.seen++;
                        if (hit.outcome === "active") {
                            tally.active++;
                            if (hit.authorTwin) tally.authorTwin++;
                            if (hit.uaTwin) tally.uaTwin++;
                        }
                        tallies.set(key, tally);
                    }
                }
            });
        }

        const stopped = (await send("CSS.stopRuleUsageTracking")) as { ruleUsage?: { styleSheetId: string; startOffset: number; endOffset: number; used: boolean }[] };
        const bySheet = new Map<string, { start: number; end: number }[]>();
        for (const usage of stopped.ruleUsage ?? []) {
            if (!usage.used) continue;
            bySheet.set(usage.styleSheetId, [...(bySheet.get(usage.styleSheetId) ?? []), { start: usage.startOffset, end: usage.endOffset }]);
        }
        let entry: { text: string; ranges: { start: number; end: number }[] } | undefined;
        for (const [styleSheetId, ranges] of bySheet) {
            const sheet = (await send("CSS.getStyleSheetText", { styleSheetId })) as { text?: string };
            if (!sheet.text) continue;
            if (sheet.text === options.css || sheet.text.startsWith(options.css.slice(0, 120))) {
                entry = { text: sheet.text, ranges };
                break;
            }
        }
        const observedKeys = new Set<string>();
        let textMatched = false;
        if (entry?.text) {
            textMatched = entry.text === options.css;
            const covered = textMatched ? parseStyleRules(options.css).rules : parseStyleRules(entry.text).rules;
            for (const rule of covered) {
                if (entry.ranges.some((range) => range.start < rule.end && rule.start < range.end)) observedKeys.add(ruleKey(rule));
            }
        }
        return { observedKeys: entry?.text ? observedKeys : null, tallies, inspected: nodeIds.length, representatives: representatives.length, truncated, textMatched };
    });
}

// --- Report --------------------------------------------------------------

function itemFromRule(rule: StyleRule, property: string, detail: string, line = rule.source?.line ?? 0, file = rule.source?.file ?? ""): Item {
    return {
        file,
        line,
        selector: normalizeSelector(rule.selector),
        property,
        detail: rule.context ? `${detail} [${normalizeSpace(rule.context)}]` : detail,
    };
}

function findingLine(rule: StyleRule, offset: number | null): { file: string; line: number } {
    const decl = offset === null ? undefined : [...rule.declarations].reverse().find((item) => item.offset <= offset);
    return { file: decl?.source?.file ?? rule.source?.file ?? "", line: decl?.source?.line ?? rule.source?.line ?? 0 };
}

function hoverUncertain(rule: StyleRule): boolean {
    return /:(?:hover|focus-visible|focus-within|focus|active)\b/.test(rule.selector);
}

export function buildReport(input: {
    rules: StyleRule[];
    structural: number;
    tokens: { classes: Set<string>; ids: Set<string> };
    pages: number;
    unresolvedIncludes: number;
    runtime: Awaited<ReturnType<typeof runRuntime>> | null;
    runtimeError: string | null;
    states: string[];
}): Report {
    const observed = input.runtime?.observedKeys;
    const notObservedAbsent: Item[] = [];
    const notObservedPresent: Item[] = [];
    const notObservedOther: Item[] = [];
    const declRule = (offset: number | null): StyleRule | undefined => {
        if (offset === null) return undefined;
        return input.rules.find((rule) => rule.start <= offset && offset < rule.end);
    };
    const talliesByRule = new Map<StyleRule, Tally[]>();
    for (const tally of input.runtime?.tallies.values() ?? []) {
        const rule = declRule(tally.offset);
        if (!rule) continue;
        talliesByRule.set(rule, [...(talliesByRule.get(rule) ?? []), tally]);
    }

    const neverWon: Item[] = [];
    const partial: Item[] = [];
    const redundantAuthor: Item[] = [];
    const redundantUa: Item[] = [];
    const sampleNote = input.runtime?.truncated ? ` Inspected ${input.runtime.inspected} of ${input.runtime.representatives} signatures; an uninspected element could still let this win.` : "";
    if (input.runtime) {
        for (const [rule, tallies] of talliesByRule) {
            const dead = tallies.filter((tally) => tally.seen > 0 && tally.active === 0);
            const live = tallies.filter((tally) => tally.active > 0);
            const where = findingLine(rule, dead[0]?.offset ?? live[0]?.offset ?? null);
            if (dead.length && !live.length) {
                const names = [...new Set(dead.map((tally) => tally.property))];
                neverWon.push(itemFromRule(rule, names.join(", "), `matched, and every inspected declaration lost the cascade.${sampleNote}`, where.line, where.file));
            } else if (dead.length && live.length) {
                partial.push(itemFromRule(rule, dead.map((tally) => tally.property).join(", "), `part of the rule still wins; these properties do not.${sampleNote}`, where.line, where.file));
            }
            for (const tally of live) {
                const at = findingLine(rule, tally.offset);
                if (tally.authorTwin === tally.active) {
                    redundantAuthor.push(itemFromRule(rule, tally.authored, `wins, but another author declaration already sets ${tally.property}: ${tally.value}. Removing both would drop the value.${sampleNote}`, at.line, at.file));
                } else if (tally.uaTwin === tally.active && tally.authorTwin === 0) {
                    redundantUa.push(itemFromRule(rule, tally.authored, `wins with the same specified value as the user-agent rule for ${tally.property}: ${tally.value}.${sampleNote}`, at.line, at.file));
                }
            }
        }
    }

    let unassessedDeclarations = 0;
    for (const rule of input.rules) {
        const status = identifierStatus(rule.selector, input.tokens);
        const seen = observed?.has(ruleKey(rule)) ?? false;
        if (observed && !seen) {
            if (input.runtime?.truncated && hoverUncertain(rule)) unassessedDeclarations += Math.max(1, rule.declarations.length);
            else if (status.kind === "absent") notObservedAbsent.push(itemFromRule(rule, "", "selector never matched, and none of its classes or ids appear as literals in templates or TypeScript"));
            else if (status.kind === "present") notObservedPresent.push(itemFromRule(rule, "", `selector never matched. Missing literals: ${status.missing.join(", ") || "none"}`));
            else notObservedOther.push(itemFromRule(rule, "", "selector never matched. It has no class or id, so a token scan cannot see it"));
        }
        if (!observed && status.kind === "absent") {
            notObservedAbsent.push(itemFromRule(rule, "", "no class or id in this selector appears as a literal. Runtime coverage did not run"));
        }
        if (observed && seen && input.runtime) {
            if (!talliesByRule.has(rule) && rule.declarations.length) unassessedDeclarations += rule.declarations.length;
        }
    }

    return {
        ruleCount: input.rules.length,
        structuralCount: input.structural,
        pages: input.pages,
        unresolvedIncludes: input.unresolvedIncludes,
        inspectedNodes: input.runtime?.inspected ?? 0,
        representativeNodes: input.runtime?.representatives ?? 0,
        truncated: input.runtime?.truncated ?? false,
        runtimeRan: input.runtime !== null,
        runtimeError: input.runtimeError,
        states: input.states,
        notObservedAbsent,
        notObservedPresent,
        notObservedOther,
        neverWon,
        partial,
        redundantAuthor,
        redundantUa,
        duplicateSelectors: duplicateSelectorFindings(input.rules),
        identicalBlocks: identicalBlockFindings(input.rules),
        unassessedDeclarations,
    };
}

function section(title: string, intro: string, items: Item[]): string {
    if (!items.length) return `## ${title}\n\nNone.\n`;
    const byFile = new Map<string, Item[]>();
    for (const item of items) {
        const file = item.file || "(no source mapping)";
        byFile.set(file, [...(byFile.get(file) ?? []), item]);
    }
    const lines = [`## ${title}`, "", intro, ""];
    for (const [file, list] of [...byFile.entries()].sort(([a], [b]) => a.localeCompare(b))) {
        lines.push(`### ${file}`, "");
        for (const item of list.sort((a, b) => a.line - b.line || a.selector.localeCompare(b.selector))) {
            const loc = item.line ? `L${item.line} ` : "";
            const prop = item.property ? ` \`${item.property}\`` : "";
            lines.push(`- ${loc}\`${item.selector}\`${prop}: ${item.detail}`);
        }
        lines.push("");
    }
    return lines.join("\n");
}

export function renderMarkdown(report: Report): string {
    const lines = [
        "# SCSS review",
        "",
        "Nothing here was deleted. Not observed means this run's DOM and states never matched the selector. A class built by concatenation, an HTMX swap that did not render, or a state this run skipped can still use it.",
        "",
        "Declaration verdicts use Chrome's matched-rule order, not a reimplemented cascade. A declaration that never wins is overridden on every inspected element. A redundant one still wins, but another declaration already sets the same specified value: delete one side only after checking the other.",
        "",
        "## How this was measured",
        "",
        "- Sass compiled in memory with source maps, so a finding points at the SCSS line.",
        "- Templates were flattened (`extends`, `include`, inclusion tags, both branches of `if`/`else`). Email templates were skipped. Variable includes were dropped.",
        "- Static literals come from templates and TypeScript. This is the token half of a PurgeCSS scan. It is not PurgeCSS, and it does not delete.",
        report.runtimeRan
            ? `- Chromium CSS rule-usage tracking and \`CSS.getMatchedStylesForNode\`, one representative per style signature (sibling index capped at 3; id values are not part of the signature). States: ${report.states.join("; ") || "none"}.`
            : `- Runtime coverage did not run${report.runtimeError ? `: ${report.runtimeError}` : ""}.`,
        report.truncated ? "- The node sample is a spread across style signatures, not every signature. Cascade findings are candidates from the inspected nodes. `:hover` / `:focus` rules that never matched were not called unobserved." : "",
        "",
        "## Summary",
        "",
        "| | |",
        "|---|---|",
        `| Style rules | ${report.ruleCount} |`,
        `| Keyframes, font faces, and other skipped at-rules | ${report.structuralCount} |`,
        `| Template fragments loaded | ${report.pages} |`,
        `| Variable includes dropped | ${report.unresolvedIncludes} |`,
        `| Element signatures / inspected | ${report.representativeNodes} / ${report.inspectedNodes} |`,
        `| Not observed, identifier absent | ${report.notObservedAbsent.length} |`,
        `| Not observed, identifier present | ${report.notObservedPresent.length} |`,
        `| Not observed, no class or id | ${report.notObservedOther.length} |`,
        `| Declarations that never win | ${report.neverWon.length} |`,
        `| Partially effective rules | ${report.partial.length} |`,
        `| Redundant with another author declaration | ${report.redundantAuthor.length} |`,
        `| Same specified value as the user agent | ${report.redundantUa.length} |`,
        `| Duplicate compiled selectors | ${report.duplicateSelectors.length} |`,
        `| Identical declaration blocks | ${report.identicalBlocks.length} |`,
        `| Declarations observed but not cascade-checked | ${report.unassessedDeclarations} |`,
        "",
    ];
    return [
        lines.filter((line) => line !== "").join("\n"),
        section("Not observed, and not a literal", "Strongest review queue. Still not a delete list: the name may be assembled in script.", report.notObservedAbsent),
        section("Not observed, but a literal exists", "The class or id is in a template or TypeScript file, and this DOM never matched the selector. Likely a dynamic state, an unresolved include, or a selector that needs a parent this flatten did not build.", report.notObservedPresent),
        section("Not observed, no class or id", "Element, pseudo, or attribute selectors coverage did not match. Check the states listed above before treating one as unused.", report.notObservedOther),
        section("Never wins the cascade", report.truncated ? "On every inspected element the selector matched, every declaration lost. The sample does not cover every signature, so treat these as candidates." : "The selector matched, and every declaration lost to another author or inline declaration on every inspected element.", report.neverWon),
        section("Partially effective", "At least one declaration still changes the cascade. The named properties do not.", report.partial),
        section("Redundant with another author rule", "Removing this declaration did not change the winning specified value, because another author rule sets it too.", report.redundantAuthor),
        section("Same value as the user agent", "The author declaration wins, and its specified value equals the user-agent value it replaced. `0` and `0px` are treated as the same. Other unit spellings are not.", report.redundantUa),
        section("Duplicate selectors", "The compiled selector appears more than once in the same at-rule context. Candidates to merge.", report.duplicateSelectors),
        section("Identical blocks", "Different selectors repeat a block of at least three declarations.", report.identicalBlocks),
    ].join("\n");
}

// --- CLI -----------------------------------------------------------------

function loadHtmlFiles(dir: string): Map<string, string> {
    const files = new Map<string, string>();
    const walk = (current: string): void => {
        for (const entry of readdirSync(current, { withFileTypes: true })) {
            const full = join(current, entry.name);
            if (entry.isDirectory()) {
                walk(full);
                continue;
            }
            if (!entry.name.endsWith(".html")) continue;
            const rel = relative(dir, full).replaceAll("\\", "/");
            if (rel.startsWith("dashboard/email/")) continue;
            files.set(rel, readFileSync(full, "utf8"));
        }
    };
    if (existsSync(dir)) walk(dir);
    return files;
}

function loadTextFiles(dir: string, extension: string): string[] {
    const texts: string[] = [];
    const walk = (current: string): void => {
        if (!existsSync(current)) return;
        for (const entry of readdirSync(current, { withFileTypes: true })) {
            const full = join(current, entry.name);
            if (entry.isDirectory()) walk(full);
            else if (entry.name.endsWith(extension)) texts.push(readFileSync(full, "utf8"));
        }
    };
    walk(dir);
    return texts;
}

function loadInclusions(): Map<string, string> {
    const map = new Map<string, string>();
    if (!existsSync(TAG_DIR)) return map;
    for (const name of readdirSync(TAG_DIR)) {
        if (!name.endsWith(".py")) continue;
        const text = readFileSync(join(TAG_DIR, name), "utf8");
        for (const match of text.matchAll(/@register\.inclusion_tag\(\s*(['"])([^'"]+)\1[\s\S]*?\bdef\s+([A-Za-z0-9_]+)\s*\(/g)) {
            map.set(match[3]!, match[2]!);
        }
    }
    return map;
}

function parseArgs(argv: string[]): { out: string | null; staticOnly: boolean; maxNodes: number; help: boolean } {
    let out: string | null = null;
    let staticOnly = false;
    let maxNodes = 400;
    let help = false;
    for (let i = 0; i < argv.length; i++) {
        const arg = argv[i];
        if (arg === "--static-only") staticOnly = true;
        else if (arg === "--help" || arg === "-h") help = true;
        else if (arg === "--out") out = argv[++i] ?? null;
        else if (arg === "--max-nodes") maxNodes = Number(argv[++i] ?? maxNodes);
    }
    return { out, staticOnly, maxNodes: Number.isFinite(maxNodes) && maxNodes > 0 ? maxNodes : 400, help };
}

async function main(): Promise<number> {
    const args = parseArgs(process.argv.slice(2));
    if (args.help) {
        console.log("Usage: bun run bin/report_unused_scss.ts [--out file.md] [--static-only] [--max-nodes N]");
        console.log("Default --max-nodes is 400. Cascade findings from a partial sample are candidates, not proof.");
        return 0;
    }
    if (!existsSync(SASS_ENTRY)) {
        console.error(`No Sass entry at ${SASS_ENTRY}`);
        return 1;
    }
    console.error("Compiling Sass…");
    const compiled = sass.compile(SASS_ENTRY, { style: "expanded", sourceMap: true });
    const css = compiled.css;
    const parsed = parseStyleRules(css);
    attachSources(parsed.rules, css, compiled.sourceMap);
    const templates = loadHtmlFiles(TEMPLATE_DIR);
    if (!templates.size) {
        console.error(`No templates under ${TEMPLATE_DIR}`);
        return 1;
    }
    console.error(`Flattening ${templates.size} templates…`);
    const rendered = renderTemplates(templates, loadInclusions());
    const tokens = collectTokens([...templates.values(), ...loadTextFiles(TS_DIR, ".ts"), ...loadTextFiles(TS_DIR, ".tsx")]);
    let runtime: Awaited<ReturnType<typeof runRuntime>> | null = null;
    let runtimeError: string | null = null;
    if (!args.staticOnly) {
        console.error("Starting Chromium…");
        try {
            runtime = await runRuntime({
                css,
                html: rendered.html,
                states: REVIEW_STATES,
                maxNodes: args.maxNodes,
                onProgress: (message) => console.error(message),
            });
            if (!runtime.observedKeys) runtimeError = "Chromium returned no coverage for the compiled sheet";
            else if (!runtime.textMatched) console.error("Coverage text differed from the compiled CSS; rules were matched by selector instead of offset.");
        } catch (error) {
            runtime = null;
            runtimeError = error instanceof Error ? error.message : String(error);
            console.error(runtimeError);
        }
    }
    const report = buildReport({
        rules: parsed.rules,
        structural: parsed.structural,
        tokens,
        pages: rendered.pages,
        unresolvedIncludes: rendered.unresolved,
        runtime,
        runtimeError,
        states: args.staticOnly ? [] : REVIEW_STATES.map((state) => state.name),
    });
    const markdown = renderMarkdown(report);
    if (args.out) {
        writeFileSync(args.out, markdown);
        console.error(`Wrote ${args.out}`);
    }
    console.log(markdown);
    return runtimeError && !args.staticOnly ? 1 : 0;
}

if (import.meta.main) {
    process.exit(await main());
}
