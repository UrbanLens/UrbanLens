/**
 * The fixture page and the real page must agree about what exists.
 *
 * Three separate defects have now come from them disagreeing: the fixture
 * pinned the map to 900px and a layout question was answered by the fixture's
 * CSS; the fixture omitted the class that makes the floor strip a column, so
 * the strip laid out sideways; and the fixture had no #floorplan-floor-fields,
 * so a panel that exists on the site was missing from every test.
 *
 * None of those were caught by a test failing. They were caught by a test
 * *passing* and the result looking wrong, which is the expensive way.
 *
 * This catches ids. It does not catch a missing *class*, which is what the
 * second one was - and that one turned out to be the cause of the first: the
 * map element had no .floorplan-map class, so the site's height rule never
 * applied, so the fixture pinned a size to compensate. Both overrides are gone
 * now and the site's stylesheet lays the fixture out alone.
 */

import { describe, expect, test } from "bun:test";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const ROOT = join(import.meta.dir, "../../../../..");
const EDITOR = join(ROOT, "src/urbanlens/dashboard/frontend/ts/entries/floorplan-editor.ts");
const TEMPLATES = join(ROOT, "src/urbanlens/dashboard/templates/dashboard");
const HARNESS = join(import.meta.dir, "harness.html");
const EDITOR_TEMPLATE = join(TEMPLATES, "pages/floorplans/editor.html");

/** Every element id the editor looks up by name. */
function idsTheEditorNeeds(): string[] {
    const source = readFileSync(EDITOR, "utf8");
    const found = new Set<string>();
    for (const match of source.matchAll(/getElementById\("([^"]+)"\)/g)) found.add(match[1] as string);
    for (const match of source.matchAll(/querySelector\("#([A-Za-z0-9_-]+)"\)/g)) found.add(match[1] as string);
    return [...found].sort();
}

/** Every template file's text, concatenated - ids may live in an include. */
function allTemplates(): string {
    const parts: string[] = [];
    const walk = (dir: string): void => {
        for (const entry of readdirSync(dir)) {
            const path = join(dir, entry);
            if (statSync(path).isDirectory()) walk(path);
            else if (entry.endsWith(".html")) parts.push(readFileSync(path, "utf8"));
        }
    };
    walk(TEMPLATES);
    return parts.join("\n");
}

/**
 * Ids the editor reads from the surrounding page rather than its own markup.
 *
 * Both are optional-chained at the call site, so their absence is a no-op
 * rather than a fault, and neither is part of what the fixture is testing.
 */
const SITE_CHROME = new Set(["map-overlays-dialog", "page-footer-attribution-text"]);

/**
 * Whether some template declares this id.
 *
 * A partial builds its ids from a parameter - `id="icon-value-{{ picker_id }}"`
 * - so the finished id never appears in any file. A leading segment followed by
 * an interpolation counts as a declaration.
 *
 * Args:
 *     id: The id the editor looks up.
 *     templates: Every template's text.
 *
 * Returns:
 *     True when some template declares it, whole or composed.
 */
function declared(id: string, templates: string): boolean {
    if (templates.includes(`"${id}"`) || templates.includes(`'${id}'`)) return true;
    // Only at a segment boundary, and only a real one. Walking every prefix
    // down to a single character would let "f{{" vouch for any id starting
    // with an f, which is a check that passes for the wrong reason - the exact
    // failure this file exists to stop.
    for (let cut = id.length; cut >= 4; cut--) {
        const prefix = id.slice(0, cut);
        if (prefix.endsWith("-") && templates.includes(`${prefix}{{`)) return true;
    }
    return false;
}

describe("the editor template's own markup", () => {
    test("every icon-only control names itself", () => {
        // The toolbar is seven buttons containing nothing but an icon glyph, so
        // without an aria-label each one announces as "button". This is read
        // from the template rather than the rendered fixture: the fixture's
        // copies of these controls are deliberately bare, and asking it would
        // be asking about the wrong markup.
        const template = readFileSync(EDITOR_TEMPLATE, "utf8");
        const buttons = template.match(/<button\b[\s\S]*?<\/button>/g) ?? [];
        expect(buttons.length).toBeGreaterThan(5);

        const unnamed = buttons
            .filter((button) => !/aria-label=/.test(button))
            // A button with its own words needs no label - but an icon's
            // ligature is not words. <i>undo</i> is how Material Symbols names
            // a glyph, and counting it as text makes this check pass for every
            // icon-only button in the file, which is all of them.
            .filter((button) => !button.replace(/<i\b[\s\S]*?<\/i>/g, "").replace(/<[^>]*>/g, "").trim())
            .map((button) => (button.match(/id="([^"]+)"|data-tool="([^"]+)"/) ?? ["(anonymous)"])[0]);

        expect(unnamed).toEqual([]);
    });
});

describe("the browser harness and the real page", () => {
    test("every id the editor reaches for exists in the templates", () => {
        // The other direction of the same check: an id the editor looks up and
        // no page provides is a lookup that silently returns null forever.
        const templates = allTemplates();
        const missing = idsTheEditorNeeds().filter((id) => !declared(id, templates));

        expect(missing).toEqual([]);
    });

    test("every id the editor reaches for exists in the harness", () => {
        const harness = readFileSync(HARNESS, "utf8");
        const missing = idsTheEditorNeeds()
            .filter((id) => !SITE_CHROME.has(id))
            .filter((id) => !harness.includes(`"${id}"`));

        expect(missing).toEqual([]);
    });
});
