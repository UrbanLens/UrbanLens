/**
 * `core.js` runs before `<body>` exists, and one throw kills every install after it.
 *
 * `themes/base.html` loads this bundle as a plain, non-deferred `<script>` inside
 * `<head>`, so `document.body` is null while its 24 `installGlobal*()` calls run.
 * They are a flat list of top-level statements: the first one that throws takes
 * the other 23 with it, silently, on every page in the app.
 *
 * That is not hypothetical. `installGlobalAssistantOverlay` began binding a
 * listener to `document.body` on 2026-09-02 (167000b77) and is the *first* call
 * in the list, so from that commit until this test was written the entire bundle
 * was dead site-wide - no confirm dialog, no fetch-json, no label picker, no
 * markup engine, no leave confirmation. Nothing failed: `window.createMarkupToolbar`
 * was simply undefined, and every feature that checked for it did nothing.
 *
 * Three modules already carry a hand-written `if (document.body)` guard and say
 * why in their own comments, so the trap was known. What was missing was anything
 * that fails when the next module forgets - which is what this is. It asserts on
 * the *last* statement in the entry, because that is the one nothing else can
 * reach if anything earlier throws.
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const BASE_TEMPLATE = join(import.meta.dir, "../../../templates/dashboard/themes/base.html");

/**
 * Run `run` in the state a `<head>` script sees: no `<body>`, still parsing.
 *
 * Both halves matter. Some modules here wait on `document.body` and some on
 * `readyState === "loading"`; either is a correct reading of "the body is not
 * there yet", and a harness that faked only one of them would report a module
 * broken for a state a browser never produces.
 */
function inHeadScriptState(run: () => void): void {
    const realBody = Object.getOwnPropertyDescriptor(Document.prototype, "body");
    const realReady = Object.getOwnPropertyDescriptor(Document.prototype, "readyState");
    Object.defineProperty(document, "body", { configurable: true, get: () => null });
    Object.defineProperty(document, "readyState", { configurable: true, get: () => "loading" });
    try {
        run();
    } finally {
        delete (document as unknown as Record<string, unknown>).body;
        delete (document as unknown as Record<string, unknown>).readyState;
        if (realBody) Object.defineProperty(Document.prototype, "body", realBody);
        if (realReady) Object.defineProperty(Document.prototype, "readyState", realReady);
    }
}

describe("core.js install list", () => {
    beforeEach(() => {
        delete (window as unknown as Record<string, unknown>).createMarkupToolbar;
    });

    afterEach(() => {
        document.body.innerHTML = "";
    });

    test("the bundle really is loaded before <body>", () => {
        // The premise. If base.html ever defers this script or moves it below the
        // fold, the rest of this file is testing a constraint that no longer binds.
        const template = readFileSync(BASE_TEMPLATE, "utf8");
        const head = template.slice(0, template.indexOf("</head>"));

        expect(head).toContain("dashboard/js/core.js");
        const tag = head.slice(head.lastIndexOf("<script", head.indexOf("dashboard/js/core.js")));
        const openingTag = tag.slice(0, tag.indexOf(">") + 1);
        expect(openingTag).not.toContain("defer");
        expect(openingTag).not.toContain('type="module"');
    });

    test("every install runs even when <body> does not exist yet", async () => {
        let thrown: unknown = null;

        inHeadScriptState(() => {
            try {
                require("./core");
            } catch (error) {
                thrown = error;
            }
        });

        expect(thrown).toBeNull();
        // The last statement in the entry: reached only if none of the 24 installs
        // above it threw.
        expect(typeof window.createMarkupToolbar).toBe("function");
    });
});
