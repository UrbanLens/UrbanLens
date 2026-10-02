/**
 * Raw fetch() calls that must stay out of the fetch net in site-runtime.ts, by marking their init `__ulReported`:
 * background requests nobody is waiting on, whose failure the user can do nothing about. Checked as source,
 * because most of them sit in entry modules that run on import.
 */
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import ts from "typescript";

const ROOT = join(import.meta.dir, "..");

interface RawFetch {
    /** The nearest named function, method or variable around the call. */
    scope: string;
    url: string;
    marked: boolean;
}

function nameOf(node: ts.Node): string | null {
    if ((ts.isFunctionDeclaration(node) || ts.isMethodDeclaration(node)) && node.name) return node.name.getText();
    if ((ts.isArrowFunction(node) || ts.isFunctionExpression(node)) && (ts.isVariableDeclaration(node.parent) || ts.isPropertyAssignment(node.parent))) {
        return node.parent.name.getText();
    }
    return null;
}

function scopeOf(node: ts.Node): string {
    for (let at: ts.Node | undefined = node.parent; at; at = at.parent) {
        const name = nameOf(at);
        if (name) return name;
    }
    return "<module>";
}

function isReportedLiteral(node: ts.Expression | undefined): boolean {
    if (!node || !ts.isObjectLiteralExpression(node)) return false;
    return node.properties.some((p) => ts.isPropertyAssignment(p) && p.name.getText() === "__ulReported" && p.initializer.kind === ts.SyntaxKind.TrueKeyword);
}

/** The initializer of the variable *name* as seen from *from*: the nearest enclosing block that declares it. */
function declaredValue(name: string, from: ts.Node): ts.Expression | undefined {
    for (let at: ts.Node | undefined = from.parent; at; at = at.parent) {
        if (!ts.isBlock(at) && !ts.isSourceFile(at)) continue;
        for (const statement of at.statements) {
            if (!ts.isVariableStatement(statement)) continue;
            const declaration = statement.declarationList.declarations.find((d) => d.name.getText() === name);
            if (declaration) return declaration.initializer;
        }
    }
    return undefined;
}

function isMarked(init: ts.Expression | undefined, call: ts.Node): boolean {
    if (init && ts.isIdentifier(init)) return isReportedLiteral(declaredValue(init.text, call));
    return isReportedLiteral(init);
}

const parsed = new Map<string, RawFetch[]>();

function rawFetches(file: string): RawFetch[] {
    const cached = parsed.get(file);
    if (cached) return cached;
    const source = ts.createSourceFile(file, readFileSync(join(ROOT, file), "utf8"), ts.ScriptTarget.Latest, true);
    const found: RawFetch[] = [];
    const visit = (node: ts.Node): void => {
        if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === "fetch") {
            found.push({ scope: scopeOf(node), url: node.arguments[0]?.getText(source) ?? "", marked: isMarked(node.arguments[1], node) });
        }
        ts.forEachChild(node, visit);
    };
    visit(source);
    parsed.set(file, found);
    return found;
}

/** [file, scope, a fragment of the URL argument, why the net must not report it]. */
const BACKGROUND: Array<[string, string, string, string]> = [
    ["entries/map-page.ts", "_recordGeolocationVisit", "_GEOLOCATION_VISIT_URL", "visit tracking on every position fix"],
    ["entries/map-page.ts", "_saveMapPosition", "settingsSaveMapPosition", "saves the view after every pan"],
    ["entries/map-page.ts", "_buildPlacesMarker", "en.wikipedia.org/api/rest_v1/page/summary", "a place without an article is a 404"],
    ["entries/map-page.ts", "_buildPlacesMarker", "mapPlacesDetails", "fills in a popup that already shows the place"],
    ["shared/safety-map.ts", "saveViewSoon", "url", "saves the view after every pan"],
];

describe("background requests stay out of the fetch net", () => {
    test.each(BACKGROUND)("%s %s (%s): %s", (file, scope, fragment) => {
        const calls = rawFetches(file).filter((call) => call.scope === scope && call.url.includes(fragment));
        expect(calls.length, `no fetch(${fragment}) in ${scope} any more`).toBeGreaterThan(0);
        for (const call of calls) expect(call.marked, `fetch(${call.url}) in ${scope} is not marked __ulReported`).toBe(true);
    });
});
