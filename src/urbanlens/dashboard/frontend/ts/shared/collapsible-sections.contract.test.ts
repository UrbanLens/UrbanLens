/**
 * Guards the contract between `window.ulSectionCollapsed` and the templates that call it.
 *
 * Lazy sections defer their fetch with `hx-trigger="load[...]"`, and the filter
 * calls a global that `core.js` installs. htmx evaluates `load` filters as soon
 * as it processes the node, which is not ordered against `core.js` executing -
 * a Playwright run against the dev environment logged 28 identical
 * `ulSectionCollapsed is not a function` errors on one Private Pin page load,
 * and every section that threw silently never loaded its content.
 *
 * So each call site has to tolerate the global being absent, and has to default
 * to *loading* when it is: a section that fetches while collapsed is invisible
 * and correct, while one that never fetches is a blank panel with no error.
 */

import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const TEMPLATES_ROOT = join(import.meta.dir, "../../../templates/dashboard");

/** Every `.html` file under the dashboard templates tree. */
function templateFiles(directory: string): string[] {
    const found: string[] = [];
    for (const entry of readdirSync(directory)) {
        const path = join(directory, entry);
        if (statSync(path).isDirectory()) found.push(...templateFiles(path));
        else if (entry.endsWith(".html")) found.push(path);
    }
    return found;
}

const callSites = templateFiles(TEMPLATES_ROOT)
    .map((path) => ({ path, source: readFileSync(path, "utf8") }))
    .filter(({ source }) => source.includes("ulSectionCollapsed"));

describe("hx-trigger call sites for ulSectionCollapsed", () => {
    test("the scan finds the call sites it is meant to guard", () => {
        // Without this, renaming the global would make every assertion below
        // pass by matching nothing.
        expect(callSites.length).toBeGreaterThan(0);
        const total = callSites.reduce((sum, { source }) => sum + (source.match(/ulSectionCollapsed/g)?.length ?? 0), 0);
        expect(total).toBeGreaterThan(10);
    });

    test("no call site invokes the global unguarded", () => {
        for (const { path, source } of callSites) {
            // `!window.ulSectionCollapsed(` - a direct call with no existence
            // check - is the form that throws when core.js has not run yet.
            expect({ path, unguarded: /!window\.ulSectionCollapsed\s*\(/.test(source) }).toEqual({ path, unguarded: false });
        }
    });

    test("every guard defaults to loading rather than to skipping", () => {
        // `window.ulSectionCollapsed && !window.ulSectionCollapsed(...)` is the
        // tempting shape and the wrong one: it evaluates falsy when the global
        // is missing, which turns a thrown error into a section that silently
        // never loads.
        for (const { path, source } of callSites) {
            expect({ path, inverted: /window\.ulSectionCollapsed\s*&&\s*!/.test(source) }).toEqual({ path, inverted: false });
        }
    });

    test("each trigger filter keeps its brackets balanced", () => {
        // htmx splits a trigger spec on top-level commas and tracks `[`/`]`
        // depth; an unbalanced filter silently swallows the triggers after it.
        for (const { path, source } of callSites) {
            for (const [filter] of source.matchAll(/load\[[^\]]*ulSectionCollapsed[^\]]*\]/g)) {
                const opens = (filter.match(/\(/g) ?? []).length;
                const closes = (filter.match(/\)/g) ?? []).length;
                expect({ path, filter, balanced: opens === closes }).toEqual({ path, filter, balanced: true });
            }
        }
    });
});
