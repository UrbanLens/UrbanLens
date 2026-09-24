/**
 * `urbanlensMediaThumbFallback` has to exist before the first `<img>` can fail.
 */

import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const REPO_ROOT = join(import.meta.dir, "..", "..", "..", "..", "..", "..");
const BASE_HTML = readFileSync(join(REPO_ROOT, "src/urbanlens/dashboard/templates/dashboard/themes/base.html"), "utf8");
const SCRIPT = readFileSync(join(REPO_ROOT, "src/urbanlens/dashboard/frontend/static/js/media-thumb-fallback.js"), "utf8");
const INCLUDE = "js/media-thumb-fallback.js";

describe("the broken-thumbnail fallback", () => {
    test("is loaded inside <head>, before any body content can fail", () => {
        const include = BASE_HTML.indexOf(INCLUDE);
        const headEnd = BASE_HTML.indexOf("</head>");

        expect(include).toBeGreaterThan(-1);
        expect(headEnd).toBeGreaterThan(-1);
        expect(include).toBeLessThan(headEnd);
    });

    test("is loaded once and defined once", () => {
        // Two definitions would mean the later one wins, and the later one is
        // the one that is too late.
        expect(BASE_HTML.split(INCLUDE).length - 1).toBe(1);
        expect(SCRIPT.split("window.urbanlensMediaThumbFallback =").length - 1).toBe(1);
    });

    test("retries an image marked data-retry-busy rather than replacing it", () => {
        // An upstream-slot refusal is a 503 that means "not yet", the same as an unfinished preview.
        const scope: { urbanlensMediaThumbFallback?: (img: unknown, icon?: string) => void } = {};
        const timers: Array<() => void> = [];
        new Function("window", "setTimeout", SCRIPT)(scope, (callback: () => void) => timers.push(callback));
        const attributes: Record<string, string> = { src: "/pin/p/immich/thumbnail/a1/", "data-retry-busy": "" };
        let replaced = false;
        const img = {
            dataset: {} as Record<string, string>,
            getAttribute: (name: string) => attributes[name] ?? null,
            hasAttribute: (name: string) => name in attributes,
            setAttribute: (name: string, value: string) => {
                attributes[name] = value;
            },
            replaceWith: () => {
                replaced = true;
            },
        };

        scope.urbanlensMediaThumbFallback?.(img, "broken_image");
        expect(replaced).toBe(false);
        expect(timers).toHaveLength(1);
        timers[0]();
        expect(attributes.src).toBe("/pin/p/immich/thumbnail/a1/?_r=1");
    });

    test("its <head> script is not deferred", () => {
        // `defer` would put it back after parsing, which is the bug.
        const include = BASE_HTML.indexOf(INCLUDE);
        const openingTag = BASE_HTML.lastIndexOf("<script", include);
        const tag = BASE_HTML.slice(openingTag, BASE_HTML.indexOf(">", include) + 1);

        expect(tag).not.toContain("defer");
        expect(tag).not.toContain("async");
        expect(tag).not.toContain("type=\"module\"");
    });
});
