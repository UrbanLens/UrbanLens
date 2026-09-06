/**
 * `urbanlensMediaThumbFallback` has to exist before the first `<img>` can fail.
 *
 * Nine templates render `<img onerror="urbanlensMediaThumbFallback(this, ...)">`
 * server-side. An `onerror` for a URL that 404s fires *during parsing*, as soon
 * as the response comes back - which for a photo whose file never landed is
 * immediately, long before the end of the document.
 *
 * `themes/base.html` defined the function in a `<script>` after the page
 * content, so every one of those handlers threw `ReferenceError` instead of
 * swapping in the icon tile the function exists to provide. Observed in a
 * browser on 2026-09-06 on Vault > Photos: four `ReferenceError`s, four broken
 * images, no fallback tiles - while `typeof window.urbanlensMediaThumbFallback`
 * read `"function"` by the time anyone looked, which is what made it invisible.
 *
 * Only handlers that can fire during parse need this. `onclick` cannot, which
 * is why the sibling `urbanlensSizeEditInPlaceInput` is fine where it is.
 */

import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const REPO_ROOT = join(import.meta.dir, "..", "..", "..", "..", "..", "..");
const BASE_HTML = readFileSync(join(REPO_ROOT, "src/urbanlens/dashboard/templates/dashboard/themes/base.html"), "utf8");

describe("the broken-thumbnail fallback", () => {
    test("is defined inside <head>, before any body content can fail", () => {
        const definition = BASE_HTML.indexOf("window.urbanlensMediaThumbFallback =");
        const headEnd = BASE_HTML.indexOf("</head>");

        expect(definition).toBeGreaterThan(-1);
        expect(headEnd).toBeGreaterThan(-1);
        expect(definition).toBeLessThan(headEnd);
    });

    test("is defined exactly once", () => {
        // Two definitions would mean the later one wins, and the later one is
        // the one that is too late.
        expect(BASE_HTML.split("window.urbanlensMediaThumbFallback =").length - 1).toBe(1);
    });

    test("its <head> script is not deferred", () => {
        // `defer` would put it back after parsing, which is the bug.
        const definition = BASE_HTML.indexOf("window.urbanlensMediaThumbFallback =");
        const openingTag = BASE_HTML.lastIndexOf("<script", definition);
        const tag = BASE_HTML.slice(openingTag, BASE_HTML.indexOf(">", openingTag) + 1);

        expect(tag).not.toContain("defer");
        expect(tag).not.toContain("async");
        expect(tag).not.toContain("type=\"module\"");
    });
});
