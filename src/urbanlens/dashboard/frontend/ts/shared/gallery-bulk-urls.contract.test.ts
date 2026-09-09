/**
 * Every surface that offers bulk delete must say whether it also offers wiki.
 *
 * `photo-context-menu.ts` and `album-items.ts` read two dataset attributes:
 * `galleryBulkUrl` gates Delete, `galleryWikiUrl` gates Send-to-wiki. They were
 * one attribute until a Vault album needed the first without the second - a
 * vault photo has no location to infer a wiki from.
 *
 * Splitting them left `_photo_gallery.html` - the flat Photos tab on a pin,
 * which is *the* place a photo gets contributed to a wiki - still emitting only
 * the first. The reader fell through to `undefined`, and the menu item stopped
 * appearing entirely: a working feature silently gone, with no error anywhere.
 *
 * So the rule is that emitting one without the other is always a mistake. An
 * empty value is a fine answer; an absent attribute is not, because absent and
 * "deliberately none" look identical to the reader and only one of them is
 * intended.
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
