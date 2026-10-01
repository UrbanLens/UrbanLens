/**
 * Guards the contract between `loadLazySection` and the templates that opt into it.
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

/** Opening tags, which may span lines, of every element carrying `marker`. */
function tagsWith(marker: string): { path: string; tag: string }[] {
    const tags: { path: string; tag: string }[] = [];
    for (const path of templateFiles(TEMPLATES_ROOT)) {
        const source = readFileSync(path, "utf8");
        for (const [tag] of source.matchAll(/<[a-z][a-z0-9-]*\b[^>]*>/gs)) {
            if (tag.includes(marker)) tags.push({ path, tag });
        }
    }
    return tags;
}

describe("lazy-loaded sections", () => {
    const lazy = tagsWith("data-ul-lazy-section");
    const triggered = tagsWith("ul:lazy-load");

    test("the scan finds the sections it is meant to guard", () => {
        expect(lazy.length).toBeGreaterThan(10);
    });

    test("every lazy section names a scope and a section", () => {
        for (const { path, tag } of lazy) {
            expect({ path, spec: /data-ul-lazy-section="[^":]+:[^":]+"/.test(tag) }).toEqual({ path, spec: true });
        }
    });

    test("every lazy section listens for ul:lazy-load, and nothing else does", () => {
        for (const { path, tag } of lazy) {
            expect({ path, tag, listens: /hx-trigger="[^"]*ul:lazy-load/.test(tag) }).toEqual({ path, tag, listens: true });
        }
        for (const { path, tag } of triggered) {
            expect({ path, tag, declared: tag.includes("data-ul-lazy-section") }).toEqual({ path, tag, declared: true });
        }
    });

    test("a lazy section still loads when restored after starting collapsed", () => {
        for (const { path, tag } of lazy) {
            expect({ path, tag, restorable: /hx-trigger="[^"]*(ul:unhide|click)/.test(tag) }).toEqual({ path, tag, restorable: true });
        }
    });
});
