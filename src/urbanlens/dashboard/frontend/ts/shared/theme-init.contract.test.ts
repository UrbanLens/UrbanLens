/**
 * The "system" theme is resolved before the first paint, from a file rather than an inline script.
 */

import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const REPO_ROOT = join(import.meta.dir, "..", "..", "..", "..", "..", "..");
const BASE_HTML = readFileSync(join(REPO_ROOT, "src/urbanlens/dashboard/templates/dashboard/themes/base.html"), "utf8");
const SCRIPT = readFileSync(join(REPO_ROOT, "src/urbanlens/dashboard/frontend/static/js/theme-init.js"), "utf8");
const INCLUDE = "js/theme-init.js";

type Listener = (event: { matches: boolean }) => void;

function run(initialTheme: string | null, prefersDark: boolean) {
    const attributes: Record<string, string> = initialTheme === null ? {} : { "data-theme": initialTheme };
    const listeners: Listener[] = [];
    const root = {
        getAttribute: (name: string) => attributes[name] ?? null,
        setAttribute: (name: string, value: string) => {
            attributes[name] = value;
        },
    };
    const document = { getElementById: (id: string) => (id === "html-root" ? root : null) };
    const window = {
        matchMedia: (query: string) => {
            expect(query).toBe("(prefers-color-scheme: dark)");
            return { matches: prefersDark, addEventListener: (_type: string, listener: Listener) => listeners.push(listener) };
        },
    };
    new Function("window", "document", SCRIPT)(window, document);
    return { theme: () => attributes["data-theme"], osChanges: (dark: boolean) => listeners.forEach((l) => l({ matches: dark })) };
}

describe("the anti-flash theme script", () => {
    test("is a synchronous script in <head>, ahead of the stylesheet it decides between", () => {
        const tag = BASE_HTML.match(/<script[^>]*js\/theme-init\.js[^>]*>/)?.[0] ?? "";
        expect(tag).not.toBe("");
        expect(tag).not.toMatch(/\b(defer|async|type="module")\b/);
        const include = BASE_HTML.indexOf(INCLUDE);
        expect(include).toBeLessThan(BASE_HTML.indexOf("dashboard/style.css"));
        expect(include).toBeLessThan(BASE_HTML.indexOf("</head>"));
        expect(BASE_HTML.split(INCLUDE).length - 1).toBe(1);
    });

    test("base.html carries no inline script", () => {
        expect(BASE_HTML.match(/<script(?![^>]*\bsrc=)[^>]*>/g)).toBeNull();
    });

    test("with no theme from the server, follows the OS, now and when it changes", () => {
        const page = run(null, true);
        expect(page.theme()).toBe("dark");
        page.osChanges(false);
        expect(page.theme()).toBe("light");
    });

    test("leaves a theme the account chose alone, even when the OS changes", () => {
        const page = run("light", true);
        expect(page.theme()).toBe("light");
        page.osChanges(true);
        expect(page.theme()).toBe("light");
    });
});
