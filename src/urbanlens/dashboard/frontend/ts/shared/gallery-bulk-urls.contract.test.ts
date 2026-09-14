/**
 * Every surface that offers bulk delete must say whether it also offers wiki.
 */

import { describe, expect, test } from "bun:test";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const REPO_ROOT = join(import.meta.dir, "..", "..", "..", "..", "..", "..");
const TEMPLATE_ROOT = join(REPO_ROOT, "src/urbanlens/dashboard/templates");

function everyTemplate(dir: string): string[] {
    return readdirSync(dir).flatMap((name) => {
        const path = join(dir, name);
        if (statSync(path).isDirectory()) return everyTemplate(path);
        return name.endsWith(".html") ? [path] : [];
    });
}

const TEMPLATES = everyTemplate(TEMPLATE_ROOT).map((path) => ({ path, source: readFileSync(path, "utf8") }));

describe("the two bulk-gallery urls", () => {
    test("no template offers one without the other", () => {
        const lopsided = TEMPLATES.filter(({ source }) => source.includes("data-gallery-bulk-url") !== source.includes("data-gallery-wiki-url")).map(({ path }) => path.slice(REPO_ROOT.length + 1));

        expect(lopsided).toEqual([]);
    });

    test("at least one template emits them, so this cannot pass by finding nothing", () => {
        const emitting = TEMPLATES.filter(({ source }) => source.includes("data-gallery-bulk-url"));

        expect(emitting.length).toBeGreaterThan(0);
    });
});
