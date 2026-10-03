import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { describe, expect, test } from "bun:test";

import * as sass from "sass";

import {
    buildReport,
    cascadeFromMatches,
    collectTokens,
    decodeMappings,
    extractBlocks,
    locate,
    parseStyleRules,
    pickRepresentatives,
    renderTemplates,
    runRuntime,
    selectorIdentifiers,
    type AuthorDecl,
    type El,
    type MatchedAuthorRule,
    type RuntimeState,
} from "./report_unused_scss";

function decl(partial: Partial<AuthorDecl> & Pick<AuthorDecl, "id" | "property" | "value">): AuthorDecl {
    return {
        important: false,
        judged: [partial.property],
        origin: "regular",
        selector: ".a",
        offset: null,
        ...partial,
    };
}

function rule(selector: string, declarations: AuthorDecl[], origin: MatchedAuthorRule["origin"] = "regular"): MatchedAuthorRule {
    return { origin, selector, matchingSelectors: [selector], declarations };
}

describe("cascadeFromMatches", () => {
    test("a later author declaration wins, and the earlier one loses", () => {
        const hits = cascadeFromMatches([
            rule(".overridden", [decl({ id: "base", property: "color", value: "red", selector: ".overridden" })]),
            rule(".overridden.extra", [decl({ id: "extra", property: "color", value: "blue", selector: ".overridden.extra" })]),
        ]);
        expect(hits.find((hit) => hit.group === "base")?.outcome).toBe("overridden");
        expect(hits.find((hit) => hit.group === "extra")?.outcome).toBe("active");
    });

    test("an earlier !important beats a later normal declaration", () => {
        const hits = cascadeFromMatches([
            rule(".a", [decl({ id: "important", property: "color", value: "red", important: true })]),
            rule(".a.b", [decl({ id: "normal", property: "color", value: "blue" })]),
        ]);
        expect(hits.find((hit) => hit.group === "important")?.outcome).toBe("active");
        expect(hits.find((hit) => hit.group === "normal")?.outcome).toBe("overridden");
    });

    test("the same specified value is a twin, not a second winner", () => {
        const hits = cascadeFromMatches([
            rule(".same", [decl({ id: "base", property: "margin-top", value: "4px", selector: ".same" })]),
            rule(".same.extra", [decl({ id: "extra", property: "margin-top", value: "4px", selector: ".same.extra" })]),
        ]);
        expect(hits.find((hit) => hit.group === "base")?.outcome).toBe("overridden");
        const winner = hits.find((hit) => hit.group === "extra");
        expect(winner?.outcome).toBe("active");
        expect(winner?.authorTwin).toBe(true);
    });

    test("a shorthand is judged by the longhands Chrome reports", () => {
        const hits = cascadeFromMatches([
            rule(".box", [decl({ id: "long", property: "margin-top", value: "3px", selector: ".box" })]),
            rule(".box.tight", [
                decl({
                    id: "short",
                    property: "margin",
                    value: "0",
                    selector: ".box.tight",
                    judged: ["margin-top", "margin-right", "margin-bottom", "margin-left"],
                }),
            ]),
        ]);
        expect(hits.find((hit) => hit.group === "long")?.outcome).toBe("overridden");
        expect(hits.filter((hit) => hit.group === "short" && hit.outcome === "active")).toHaveLength(4);
    });

    test("a user-agent value with the same spelling is a ua twin", () => {
        const hits = cascadeFromMatches([
            rule("div", [decl({ id: "ua", property: "display", value: "block", origin: "user-agent", selector: "div" })], "user-agent"),
            rule("div", [decl({ id: "author", property: "display", value: "block", selector: "div" })]),
        ]);
        const author = hits.find((hit) => hit.group === "author");
        expect(author?.outcome).toBe("active");
        expect(author?.uaTwin).toBe(true);
    });
});

describe("templates and selectors", () => {
    test("extends, include, and both if branches survive", () => {
        const files = new Map([
            ["base.html", "<body class=\"app\">{% block content %}X{% endblock %}</body>"],
            ["page.html", "{% extends 'base.html' %}{% block content %}<div class=\"page\">{% include 'part.html' %}</div>{% endblock %}"],
            ["part.html", "<span class=\"part\">{% if a %}A{% else %}B{% endif %}</span>"],
            ["orphan.html", "<i class=\"orphan\"></i>"],
        ]);
        const rendered = renderTemplates(files, new Map());
        expect(rendered.pages).toBe(2);
        expect(rendered.html).toContain("page");
        expect(rendered.html).toContain("part");
        expect(rendered.html).toContain(">AB<");
        expect(rendered.html).toContain("orphan");
        expect(rendered.html).not.toContain(">X<");
        expect(rendered.html).toContain('class="app"');
    });

    test("a nested block can be replaced without replacing its parent", () => {
        const extracted = extractBlocks("{% block a %}A{% block b %}B{% endblock %}{% endblock %}");
        extracted.blocks.set("b", "B2");
        const html = extracted.skeleton.replace(/%%BLOCK:([A-Za-z0-9_]+)%%/g, (_, name: string) => extracted.blocks.get(name) ?? "");
        const done = html.replace(/%%BLOCK:([A-Za-z0-9_]+)%%/g, (_, name: string) => extracted.blocks.get(name) ?? "");
        expect(done).toBe("AB2");
    });

    test("class and id tokens ignore attribute selectors", () => {
        expect(selectorIdentifiers(".btn--primary #map[href='#x']")).toEqual({ classes: ["btn--primary"], ids: ["map"] });
    });

    test("quoted script strings count as literals, so a dynamic class is not absent", () => {
        const tokens = collectTokens(['<div class="static">', 'el.classList.add("from-script")']);
        expect(tokens.classes.has("static")).toBe(true);
        expect(tokens.classes.has("from-script")).toBe(true);
    });
});

describe("representatives", () => {
    test("repeated page chrome collapses and a different class does not", () => {
        const header = (page: string): El => ({
            nodeId: page === "a" ? 1 : 2,
            tag: "div",
            attrs: { "data-ul-page": page },
            children: [
                { nodeId: page === "a" ? 10 : 20, tag: "header", attrs: { class: "site" }, children: [] },
                { nodeId: page === "a" ? 11 : 21, tag: "main", attrs: { class: page === "a" ? "one" : "two" }, children: [] },
            ],
        });
        const ids = pickRepresentatives([header("a"), header("b")]).sort((a, b) => a - b);
        expect(ids).toEqual([10, 11, 21]);
    });

    test("identical rows collapse after the fourth, and a different class does not", () => {
        const items: El[] = Array.from({ length: 20 }, (_, index) => ({
            nodeId: index + 1,
            tag: "li",
            attrs: { class: index === 10 ? "other" : "row", id: `row-${index}` },
            children: [],
        }));
        const root: El = { nodeId: 100, tag: "ul", attrs: {}, children: items };
        expect(pickRepresentatives([root])).toEqual([100, 1, 2, 3, 4, 11]);
    });
});

describe("source maps", () => {
    test("a compiled rule points back at the scss line", () => {
        const result = sass.compileString(".btn {\n  color: red;\n}\n", {
            syntax: "scss",
            sourceMap: true,
            url: new URL("file:///C:/fixture.scss"),
        });
        const { rules } = parseStyleRules(result.css);
        const color = rules[0]?.declarations[0];
        expect(color?.property).toBe("color");
        const point = locate(result.css, result.sourceMap, result.css.indexOf("color"));
        expect(point?.line).toBe(2);
        expect(point?.file.toLowerCase()).toContain("fixture.scss");
        expect(decodeMappings(result.sourceMap?.mappings ?? "").length).toBeGreaterThan(0);
    });
});

describe("buildReport", () => {
    test("an absent selector is not called unused when runtime did not run", () => {
        const css = ".missing { color: red; }\n.used { color: blue; }\n";
        const { rules } = parseStyleRules(css);
        const report = buildReport({
            rules,
            structural: 0,
            tokens: collectTokens(['<div class="used">']),
            pages: 1,
            unresolvedIncludes: 0,
            runtime: null,
            runtimeError: null,
            states: [],
        });
        expect(report.notObservedAbsent.map((item) => item.selector)).toEqual([".missing"]);
        expect(report.runtimeRan).toBe(false);
        expect(report.neverWon).toEqual([]);
    });

    test("a partial sample still lists a declaration that lost on every inspected node", () => {
        const css = ".overridden { color: red; }\n";
        const { rules } = parseStyleRules(css);
        const declOffset = rules[0]?.declarations[0]?.offset ?? 0;
        const report = buildReport({
            rules,
            structural: 0,
            tokens: collectTokens(['<div class="overridden">']),
            pages: 1,
            unresolvedIncludes: 0,
            runtime: {
                observedKeys: new Set([".overridden@@"]),
                tallies: new Map([
                    [
                        "color",
                        {
                            group: "g",
                            property: "color",
                            authored: "color",
                            value: "red",
                            selector: ".overridden",
                            offset: declOffset,
                            seen: 2,
                            active: 0,
                            authorTwin: 0,
                            uaTwin: 0,
                        },
                    ],
                ]),
                inspected: 10,
                representatives: 40,
                truncated: true,
                textMatched: true,
            },
            runtimeError: null,
            states: ["light 1280"],
        });
        expect(report.neverWon.map((item) => item.selector)).toEqual([".overridden"]);
        expect(report.neverWon[0]?.detail).toContain("10 of 40");
        expect(report.notObservedAbsent).toEqual([]);
    });
});

const FIXTURE_CSS = [
    ".live { color: green; }",
    ".overridden { color: red; }",
    ".overridden.extra { color: blue; }",
    ".partial { color: red; outline-style: solid; }",
    ".partial.extra { color: blue; }",
    ".same { margin-top: 4px; }",
    ".same.extra { margin-top: 4px; }",
    ".missing { color: orange; }",
    ".hover-only:hover { color: purple; }",
    "div { display: block; }",
    ".box { margin-top: 3px; }",
    ".box.tight { margin: 0; }",
].join("\n");

const FIXTURE_HTML = [
    '<div class="live"></div>',
    '<div class="overridden extra"></div>',
    '<div class="partial extra"></div>',
    '<div class="same extra"></div>',
    '<div class="hover-only"></div>',
    '<div class="box tight"></div>',
].join("");

const FIXTURE_STATES: RuntimeState[] = [
    { name: "rest", width: 1280, theme: "light", pseudos: [], features: [], sample: true },
    { name: "hover", width: 1280, theme: "light", pseudos: ["hover"], features: [], sample: true },
];

describe("chromium", () => {
    test("coverage and matched styles separate unobserved, overridden, partial, and redundant rules", async () => {
        // happy-dom's WebSocket cannot open a CDP socket. Sass still needs the DOM, so put it back after.
        GlobalRegistrator.unregister();
        try {
            const runtime = await runRuntime({ css: FIXTURE_CSS, html: FIXTURE_HTML, states: FIXTURE_STATES, maxNodes: 50 });
            const { rules } = parseStyleRules(FIXTURE_CSS);
            const report = buildReport({
                rules,
                structural: 0,
                tokens: collectTokens([FIXTURE_HTML]),
                pages: 1,
                unresolvedIncludes: 0,
                runtime,
                runtimeError: runtime.observedKeys ? null : "no coverage",
                states: FIXTURE_STATES.map((state) => state.name),
            });
            expect(runtime.observedKeys).not.toBeNull();
            expect(runtime.textMatched).toBe(true);
            const selectors = (items: { selector: string }[]) => items.map((item) => item.selector);

            expect(selectors(report.notObservedAbsent)).toContain(".missing");
            expect(selectors(report.notObservedAbsent)).not.toContain(".live");
            expect(selectors(report.neverWon)).toContain(".overridden");
            expect(selectors(report.neverWon)).not.toContain(".live");
            expect(selectors(report.neverWon)).not.toContain("div");
            expect(selectors(report.partial)).toContain(".partial");
            expect(report.partial.find((item) => item.selector === ".partial")?.property).toContain("color");
            expect(selectors(report.redundantAuthor).some((selector) => selector.includes(".same.extra"))).toBe(true);
            expect(selectors([...report.notObservedAbsent, ...report.notObservedPresent])).not.toContain(".hover-only:hover");
            expect(selectors(report.neverWon)).not.toContain(".hover-only:hover");

            const box = report.neverWon.find((item) => item.selector === ".box");
            const boxPartial = report.partial.find((item) => item.selector === ".box");
            expect(box ?? boxPartial).toBeTruthy();
        } finally {
            GlobalRegistrator.register({ url: "https://urbanlens.test/" });
        }
    }, 60_000);
});
