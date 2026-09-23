/**
 * Every value interpolated into markup must be escaped, validated, or reviewed.
 *
 * "Markup" is any template literal or `+` chain whose static text holds a tag, plus anything assigned to
 * `innerHTML`/`outerHTML` or passed to `insertAdjacentHTML`. A local variable is followed to every value it is
 * given, so markup built in a variable and interpolated later is checked where it is built (P128: an
 * `iconHtml` built from a raw label icon passed because the name `iconHtml` was approved everywhere).
 */

import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import ts from "typescript";

const TS_ROOT = join(import.meta.dir, "..");

/** Calls whose result is safe in markup: escapers, colour/number validators, and numeric conversions. */
const SAFE_CALLS = new Set([
    "escHtml",
    "_escHtml",
    "escapeHtml",
    "_escapeHtml",
    "escapeAttr",
    "_ulEscText",
    "_ulEscAttr",
    "escapeMarkupLabel",
    "safeColor",
    "_safePinColor",
    "_resolvePinColor",
    "safeNumber",
    "_hexToRgba",
    "hexToRgb",
    "toFixed",
    "parseInt",
    "parseFloat",
    "Number",
]);

const ARITHMETIC = new Set([ts.SyntaxKind.MinusToken, ts.SyntaxKind.AsteriskToken, ts.SyntaxKind.SlashToken, ts.SyntaxKind.PercentToken]);

const MARKUP = /<[a-zA-Z!/]/;

/**
 * Interpolations reviewed and found safe, keyed `file: expression` so an approval covers one file only.
 */
const REVIEWED_SAFE = new Map<string, string>([
    // Developer-authored constants.
    ["entries/article-wysiwyg.ts: def.icon", "static toolbar definition (BUBBLE_BUTTONS)"],
    ["entries/article-wysiwyg.ts: item.icon", "static slash-command definition (SLASH_ITEMS)"],
    ["entries/article-wysiwyg.ts: item.label", "static slash-command definition (SLASH_ITEMS)"],
    ["entries/map-page.ts: src.cls", "_PLACES_SOURCE_ICONS constant"],
    ["entries/map-page.ts: src.icon", "_PLACES_SOURCE_ICONS constant"],
    ["shared/onboarding-tour.ts: card.icon", "cards are literals in entries/organize.ts"],
    ["shared/onboarding-tour.ts: card.eyebrow", "cards are literals in entries/organize.ts"],
    ["shared/onboarding-tour.ts: card.title", "cards are literals in entries/organize.ts"],
    ["shared/onboarding-tour.ts: card.body", "cards are literals in entries/organize.ts; may carry markup"],
    ["shared/onboarding-tour.ts: card.button", "cards are literals in entries/organize.ts"],
    ["shared/photo-context-menu.ts: action.icon", "action list is literals in the same module"],
    ["shared/organize-tab-manager.ts: this.cfg.emptyIcon", "tab config literals in entries/organize.ts"],
    ["shared/organize-tab-manager.ts: this.cfg.entitySingular", "tab config literals in entries/organize.ts"],
    ["shared/organize-tab-manager.ts: this.cfg.convertTargets.find((t) => t.kind === this.convertTarget)?.label", "tab config literals in entries/organize.ts"],
    ["shared/organize-filter-engine.ts: p.label", "NS_LABELS entry"],
    ["shared/organize-filter-engine.ts: p.ns", "namespace key from the same module's fixed list"],
    ["shared/label-picker.ts: mode", "ChipMode union, \"incl\" | \"excl\""],
    ["shared/label-picker.ts: word", "\"AND\" | \"OR\" | \"NOT\" by type"],
    ["shared/e2ee-client.ts: config?.urls.faqUrl", "server-rendered reverse() URL from the page config"],
    // Numbers, ids and slugs the server generates.
    ["entries/map-page.ts: i", "index over the literal [1, 2, 3, 4, 5]"],
    ["entries/map-page.ts: pin.id", "integer primary key"],
    ["entries/map-page.ts: pin.uuid", "server-generated UUID"],
    ["entries/map-page.ts: uuid", "pin UUID key of the merge selection"],
    ["entries/map-page.ts: pin.slug", "server-generated slug, [-a-z0-9]"],
    ["entries/map-page.ts: pin.child_count", "integer count"],
    ["entries/map-page.ts: token", "server-generated undo token"],
    ["shared/label-picker.ts: i", "loop index"],
    ["shared/markup-engine.ts: sz", "number parameter"],
    ["shared/markup-toolbar.ts: rect.h", "computed pixel size"],
    ["shared/markup-toolbar.ts: rect.w", "computed pixel size"],
    ["shared/markup-toolbar.ts: textFontSize(item)", "number"],
    ["shared/organize-tab-manager.ts: data.id", "integer id from the server-rendered card's data attribute"],
    ["shared/organize-tab-manager.ts: data.pinCount", "integer count from the server-rendered card's data attribute"],
    ["shared/organize-tab-manager.ts: data.locationCount", "integer count from the server-rendered card's data attribute"],
    ["shared/organize-filter-engine.ts: p.n", "integer count"],
    ["shared/photo-tile.ts: tile.id", "integer id"],
    // Validated before use.
    ["entries/map-page.ts: _normalizeHexColor(color)", "color is _resolvePinColor's validated hex or a literal"],
    ["entries/map-page.ts: props.osm_url", "used only after an https://www.openstreetmap.org/ prefix check"],
    ["shared/markup-toolbar.ts: itemColor(item)", "safeColor"],
    ["shared/markup-toolbar.ts: textBackground(item)", "safeColor or a literal"],
    ["entries/map-page.ts: _pinCardIconHtml(pin)", "escapes the icon in every branch"],
    // Escaped inline rather than through a named escaper.
    ['shared/mention-autocomplete.ts: item.name.replace(/&/g, "&amp;").replace(/</g, "&lt;")', "element content, not an attribute"],
    [
        'shared/markup-engine.ts: String(s.label ?? "").replace(/[&<>"\']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", \'"\': "&quot;", "\'": "&#39;" })[c]!)',
        "escapes all five characters",
    ],
    // FileReader data: URL of the user's own just-selected file; base64 cannot contain a quote.
    ["entries/map-page.ts: String(e.target?.result)", "FileReader data URL"],
    ["shared/organize-icon-picker.ts: e.target?.result", "FileReader data URL"],
]);

function tsFiles(dir: string): string[] {
    const out: string[] = [];
    for (const entry of readdirSync(dir)) {
        const full = join(dir, entry);
        if (statSync(full).isDirectory()) {
            out.push(...tsFiles(full));
        } else if (entry.endsWith(".ts") && !entry.endsWith(".test.ts") && !entry.endsWith(".d.ts")) {
            out.push(full);
        }
    }
    return out;
}

function calleeName(callee: ts.Expression): string {
    if (ts.isIdentifier(callee)) return callee.text;
    if (ts.isPropertyAccessExpression(callee)) return callee.name.text;
    return "";
}

const NUMERIC_TYPES = new Set([ts.SyntaxKind.NumberKeyword, ts.SyntaxKind.BooleanKeyword]);

/**
 * Every value a local variable can hold: its initializer and, for a `let`, each assignment in its block.
 * A parameter declared `number` or `boolean` holds nothing that can be markup, so it binds no values.
 */
function bindings(id: ts.Identifier): ts.Expression[] | undefined {
    for (let node: ts.Node | undefined = id.parent; node; node = node.parent) {
        if (ts.isFunctionLike(node)) {
            const parameter = node.parameters.find((p) => ts.isIdentifier(p.name) && p.name.text === id.text);
            if (parameter) return parameter.type && NUMERIC_TYPES.has(parameter.type.kind) ? [] : undefined;
        }
        if (!(ts.isBlock(node) || ts.isSourceFile(node) || ts.isModuleBlock(node) || ts.isCaseClause(node) || ts.isDefaultClause(node))) continue;
        for (const statement of node.statements) {
            if (!ts.isVariableStatement(statement)) continue;
            const declaration = statement.declarationList.declarations.find((d) => ts.isIdentifier(d.name) && d.name.text === id.text);
            if (!declaration) continue;
            const values = declaration.initializer ? [declaration.initializer] : [];
            if (!(statement.declarationList.flags & ts.NodeFlags.Const)) {
                const scope = node;
                const collect = (n: ts.Node): void => {
                    if (
                        ts.isBinaryExpression(n) &&
                        ts.isIdentifier(n.left) &&
                        n.left.text === id.text &&
                        (n.operatorToken.kind === ts.SyntaxKind.EqualsToken || n.operatorToken.kind === ts.SyntaxKind.PlusEqualsToken)
                    ) {
                        values.push(n.right);
                    }
                    ts.forEachChild(n, collect);
                };
                collect(scope);
            }
            return values.length ? values : undefined;
        }
    }
    return undefined;
}

/** What a callback passed to `.map()` returns. */
function returnedBy(fn: ts.Expression | undefined): ts.Expression[] | undefined {
    if (!fn || !(ts.isArrowFunction(fn) || ts.isFunctionExpression(fn))) return undefined;
    if (!ts.isBlock(fn.body)) return [fn.body];
    const out: ts.Expression[] = [];
    const walk = (n: ts.Node): void => {
        if (ts.isReturnStatement(n) && n.expression) out.push(n.expression);
        else if (!ts.isFunctionLike(n)) ts.forEachChild(n, walk);
    };
    walk(fn.body);
    return out;
}

/** Array and string methods whose result holds only what their receiver and arguments held. */
const PASS_THROUGH = new Set(["join", "slice", "concat", "trim", "toLowerCase", "toUpperCase"]);

/**
 * The sub-expressions of *e* that could put unescaped text into markup.
 *
 * @param e - The interpolated expression.
 * @param resolving - Variables already being followed, so a `let` that appends to itself terminates.
 */
function unsafeLeaves(e: ts.Expression, resolving: ReadonlySet<string> = new Set()): ts.Expression[] {
    const recurse = (next: ts.Expression): ts.Expression[] => unsafeLeaves(next, resolving);
    if (ts.isStringLiteral(e) || ts.isNoSubstitutionTemplateLiteral(e) || ts.isNumericLiteral(e)) return [];
    if (ts.isPrefixUnaryExpression(e) && ts.isNumericLiteral(e.operand)) return [];
    if (ts.isObjectLiteralExpression(e)) {
        return e.properties.flatMap((property) => (ts.isPropertyAssignment(property) ? recurse(property.initializer) : [e]));
    }
    if (ts.isIdentifier(e)) {
        if (resolving.has(e.text)) return [];
        const values = bindings(e);
        const inner = new Set([...resolving, e.text]);
        return values ? values.flatMap((v) => unsafeLeaves(v, inner)) : [e];
    }
    if (ts.isParenthesizedExpression(e) || ts.isNonNullExpression(e)) return recurse(e.expression);
    if (ts.isElementAccessExpression(e)) return ts.isIdentifier(e.expression) && bindings(e.expression) ? recurse(e.expression) : [e];
    if (ts.isTemplateExpression(e)) return e.templateSpans.flatMap((span) => recurse(span.expression));
    if (ts.isConditionalExpression(e)) return [...recurse(e.whenTrue), ...recurse(e.whenFalse)];
    if (ts.isBinaryExpression(e)) {
        const op = e.operatorToken.kind;
        if (ARITHMETIC.has(op)) return [];
        if (op === ts.SyntaxKind.AmpersandAmpersandToken) return recurse(e.right);
        if (op === ts.SyntaxKind.BarBarToken || op === ts.SyntaxKind.QuestionQuestionToken || op === ts.SyntaxKind.PlusToken) {
            return [...recurse(e.left), ...recurse(e.right)];
        }
    }
    if (ts.isCallExpression(e)) {
        const name = calleeName(e.expression);
        if (SAFE_CALLS.has(name)) return [];
        // `xs.map((x) => `<li>${...}</li>`)` holds whatever the callback returns.
        if (name === "map" && ts.isPropertyAccessExpression(e.expression)) {
            const bodies = returnedBy(e.arguments[0]);
            if (bodies) return bodies.flatMap(recurse);
        }
        if (PASS_THROUGH.has(name) && ts.isPropertyAccessExpression(e.expression)) {
            return [...recurse(e.expression.expression), ...e.arguments.flatMap(recurse)];
        }
    }
    return [e];
}

function staticText(e: ts.Expression): string {
    if (ts.isTemplateExpression(e)) return e.head.text + e.templateSpans.map((span) => span.literal.text).join("");
    if (ts.isStringLiteral(e) || ts.isNoSubstitutionTemplateLiteral(e)) return e.text;
    if (ts.isParenthesizedExpression(e)) return staticText(e.expression);
    if (ts.isBinaryExpression(e) && e.operatorToken.kind === ts.SyntaxKind.PlusToken) return staticText(e.left) + staticText(e.right);
    return "";
}

function isHtmlSink(node: ts.Node): boolean {
    const parent = node.parent;
    if (ts.isBinaryExpression(parent) && parent.right === node && ts.isPropertyAccessExpression(parent.left)) {
        const assigns = parent.operatorToken.kind === ts.SyntaxKind.EqualsToken || parent.operatorToken.kind === ts.SyntaxKind.PlusEqualsToken;
        return assigns && ["innerHTML", "outerHTML"].includes(parent.left.name.text);
    }
    return ts.isCallExpression(parent) && calleeName(parent.expression) === "insertAdjacentHTML" && parent.arguments[1] === node;
}

function isConcatRoot(node: ts.Node): node is ts.BinaryExpression {
    if (!ts.isBinaryExpression(node) || node.operatorToken.kind !== ts.SyntaxKind.PlusToken) return false;
    const parent = node.parent;
    return !(ts.isBinaryExpression(parent) && parent.operatorToken.kind === ts.SyntaxKind.PlusToken) && !ts.isParenthesizedExpression(parent);
}

interface Interpolation {
    key: string;
    where: string;
}

function interpolations(): Interpolation[] {
    const found: Interpolation[] = [];
    for (const file of tsFiles(TS_ROOT)) {
        const rel = file.slice(TS_ROOT.length + 1);
        const source = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true);
        const seen = new Set<number>();
        const visit = (node: ts.Node): void => {
            const markup = ts.isTemplateExpression(node) || isConcatRoot(node);
            if (markup && (MARKUP.test(staticText(node as ts.Expression)) || isHtmlSink(node))) {
                for (const leaf of unsafeLeaves(node as ts.Expression)) {
                    if (seen.has(leaf.pos)) continue;
                    seen.add(leaf.pos);
                    const line = source.getLineAndCharacterOfPosition(leaf.getStart()).line + 1;
                    found.push({ key: `${rel}: ${leaf.getText().replace(/\s+/g, " ")}`, where: `${rel}:${line}` });
                }
            }
            ts.forEachChild(node, visit);
        };
        visit(source);
    }
    return found;
}

describe("markup interpolations are escaped", () => {
    const found = interpolations();

    test("every interpolated value is escaped, validated or reviewed", () => {
        const unreviewed = found.filter(({ key }) => !REVIEWED_SAFE.has(key)).map(({ key, where }) => `${where}  ${key}`);

        expect([...new Set(unreviewed)].sort()).toEqual([]);
    });

    test("the scan actually finds interpolations", () => {
        // Every assertion above passes trivially if the walker stops matching.
        expect(found.length).toBeGreaterThan(40);
    });

    test("the allowlist has no stale entries", () => {
        // An entry that no longer appears means the code moved on and the exemption is now unexamined cover for
        // whatever replaces it.
        const present = new Set(found.map(({ key }) => key));

        expect([...REVIEWED_SAFE.keys()].filter((key) => !present.has(key))).toEqual([]);
    });

    test("a label icon interpolated through a local variable is caught (P128)", () => {
        const source = ts.createSourceFile(
            "probe.ts",
            'function chip(b) { const iconHtml = b.icon ? `<span>${b.icon}</span>` : ""; el.innerHTML = `${iconHtml}<span>${escHtml(b.name)}</span>`; }',
            ts.ScriptTarget.Latest,
            true,
        );
        const leaves: string[] = [];
        const visit = (node: ts.Node): void => {
            if (ts.isBinaryExpression(node) && ts.isPropertyAccessExpression(node.left) && node.left.name.text === "innerHTML") {
                leaves.push(...unsafeLeaves(node.right).map((leaf) => leaf.getText()));
            }
            ts.forEachChild(node, visit);
        };
        visit(source);

        expect(leaves).toEqual(["b.icon"]);
    });
});
