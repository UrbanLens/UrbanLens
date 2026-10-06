/**
 * The site's stylesheet on native form controls, in a real browser.
 *
 * A `<select>`'s popup is the browser's own, drawn from the colours its `<option>` rows carry, so only a browser shows
 * whether the rows are legible. The popup once came up light text on a light grey row in the dark theme.
 */

import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { existsSync } from "node:fs";
import { join } from "node:path";

import { type Browser, chromium } from "playwright";

// See floorplan-editor.test.ts: the happy-dom preload is wrong for a file that drives a real browser.
if (GlobalRegistrator.isRegistered) await GlobalRegistrator.unregister();

const ROOT = join(import.meta.dir, "../../../../..");
const STYLE = join(ROOT, "src/urbanlens/dashboard/frontend/static/dashboard/style.css");

/** These read the built stylesheet, so they need `bun run sass` to have run. */
const BUILT = existsSync(STYLE);

/** WCAG's AA threshold for text under 18pt. */
const AA = 4.5;

function page(theme: "light" | "dark"): string {
    return `<!doctype html><html lang="en" data-theme="${theme}"><head><meta charset="utf-8"><link rel="stylesheet" href="/dashboard/style.css"></head>
<body><div class="container"><select id="pref"><option value="">---------</option><option value="y">Yes, please.</option>
<optgroup label="Other"><option value="o">Other (please specify below)</option></optgroup></select></div></body></html>`;
}

/** WCAG 2.x relative luminance of an `rgb()`/`rgba()` computed colour. */
function luminance(color: string): number {
    const [r, g, b] = color.match(/[\d.]+/g)!.slice(0, 3).map((c) => {
        const v = Number(c) / 255;
        return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
    }) as [number, number, number];
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
    const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
    return (hi + 0.05) / (lo + 0.05);
}

let browser: Browser;
let server: ReturnType<typeof Bun.serve>;

beforeAll(async () => {
    if (!BUILT) return;
    const libs = join(process.env.HOME || "", "browserlibs/root/usr/lib/x86_64-linux-gnu");
    process.env.LD_LIBRARY_PATH = process.env.LD_LIBRARY_PATH ? `${process.env.LD_LIBRARY_PATH}:${libs}` : libs;
    browser = await chromium.launch({ args: ["--no-sandbox", "--disable-gpu"] });
    server = Bun.serve({
        port: 0,
        fetch(request) {
            const url = new URL(request.url);
            if (url.pathname === "/dashboard/style.css") return new Response(Bun.file(STYLE), { headers: { "content-type": "text/css" } });
            return new Response(page(url.searchParams.get("theme") === "dark" ? "dark" : "light"), { headers: { "content-type": "text/html" } });
        },
    });
});

afterAll(async () => {
    await browser?.close();
    server?.stop(true);
});

describe.skipIf(!BUILT)("native <select> option rows", () => {
    for (const theme of ["light", "dark"] as const) {
        test(`carry an opaque background and a legible colour in the ${theme} theme, which the translucent select cannot give them`, async () => {
            const context = await browser.newContext();
            const tab = await context.newPage();
            await tab.goto(`${server.url}/?theme=${theme}`);
            const rows = await tab.evaluate(() =>
                [...document.querySelectorAll("option, optgroup")].map((el) => {
                    const cs = getComputedStyle(el);
                    return { background: cs.backgroundColor, color: cs.color };
                }),
            );
            const select = await tab.evaluate(() => getComputedStyle(document.getElementById("pref")!).backgroundColor);
            // What the translucent select paints is not what a popup row falls back on, so the rows must not rely on it.
            expect(select).toMatch(/^rgba\(.*, 0\.\d+\)$/);
            expect(rows).toHaveLength(4);
            for (const row of rows) {
                expect(row.background).toMatch(/^rgb\(/);
                expect(contrast(row.background, row.color)).toBeGreaterThanOrEqual(AA);
            }
            await context.close();
        });
    }

    test("the dark theme declares itself a dark colour scheme, for the popup's own scrollbar and chrome", async () => {
        const context = await browser.newContext();
        const tab = await context.newPage();
        await tab.goto(`${server.url}/?theme=dark`);
        expect(await tab.evaluate(() => getComputedStyle(document.documentElement).colorScheme)).toBe("dark");
        await context.close();
    });
});
