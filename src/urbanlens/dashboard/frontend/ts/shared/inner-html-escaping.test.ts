/**
 * Every value interpolated into an `innerHTML` template literal must be escaped.
 *
 * The modules that build markup as strings all define an `escHtml` and use it -
 * `map-annotations.ts` in six places, `organize-tab-manager.ts` in two - but
 * "usually escaped" is not a property, and two sites had quietly skipped it:
 * both interpolated a URL straight into an `src="..."` attribute, where a quote
 * would close the attribute rather than sit inside it. Neither was shown to be
 * exploitable (the values come from Django-generated media paths, and filename
 * sanitisation strips quotes), which is exactly why nothing had noticed them.
 *
 * This encodes the audit rather than the two fixes: every interpolation is
 * either escaped, or listed below with the reason it is safe. A new unescaped
 * one fails here instead of waiting for a value that finally contains a quote.
 *
 * The allowlist is deliberately expression-text, not file-scoped. Renaming a
 * variable drops it out of the list and forces it to be looked at again, which
 * is the intended cost.
 */

import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const TS_ROOT = join(import.meta.dir, "..");

/**
 * Interpolations reviewed and found safe, with why.
 *
 * Keep this short. An entry here is a claim that the value cannot carry markup,
 * and each one was checked against its source, not assumed.
 */
const REVIEWED_SAFE = new Map<string, string>([
    // Developer-authored constants: BUBBLE_BUTTONS / SLASH_ITEMS in article-wysiwyg.ts.
    ["def.icon", "static toolbar definition"],
    ["item.icon", "static slash-command definition"],
    ["item.label", "static slash-command definition"],
    // Static onboarding card list in entries/organize.ts.
    ["card.icon", "static onboarding card definition"],
    // Material-symbol names from the action list built in photo-context-menu.ts
    // itself - every icon there is a developer-authored literal.
    ["action.icon", "static context-menu action definition"],
    // Literal branches - no external value reaches the markup.
    ['converting ? "Converting…" : "Saving…"', "string literals"],
    ['hasCoords ? "Has GPS" : "No GPS"', "string literals"],
    ['hasCoords ? "Move on map" : "Place on map"', "string literals"],
    ['hasCoords ? "has-gps" : "no-gps"', "string literals"],
    ['hasCoords ? "place" : "location_off"', "string literals"],
    // Was an inline ternary of the same literals; upstream hoisted it to `intro`
    // and split the passkey markup into `passkeyBlock`. Both are ternaries over
    // developer-authored strings - `passkeyBlock` between a fixed HTML literal and
    // "" - so no external value reaches the markup either way.
    ["intro", "ternary over string literals"],
    ["passkeyBlock", "ternary between a static HTML literal and empty string"],
    // Escaped inline rather than via escHtml.
    ['item.name.replace(/&/g, "&amp;").replace(/</g, "&lt;")', "escaped inline; element content, not an attribute"],
    // Numbers.
    ["seq", "monotonic integer"],
    // Markup assembled by the same module, whose own interpolations this test also checks.
    ["iconHtml", "pre-built markup"],
    ["layerPicker", "pre-built markup"],
    ["meta", "pre-built markup"],
    ["owner", "pre-built markup"],
    ["ownerMeta", "pre-built markup"],
    ["partsHtml", "pre-built markup from counts and static labels"],
    ["passwordField", "pre-built markup"],
    ["faqLink", "pre-built markup"],
    ["subtitle", "pre-built markup"],
    ["prefix", "static namespace label"],
    // photo-tile.ts fragments, each a literal or "" chosen by a boolean. The
    // only value any of them interpolates is `tile.id`, which both tile parsers
    // coerce with Number()/parseInt before it can reach markup.
    ["check", "static markup or empty string"],
    ["remove", "static markup around a numeric id"],
    ["caption", "empty <p> or empty string; its text is set via textContent"],
    // FileReader data: URL of the user's own just-selected file; base64 payload
    // cannot contain a quote, and it never leaves this browser.
    ["e.target?.result", "FileReader data URL"],
]);

const INNER_HTML_TEMPLATE = /innerHTML\s*=\s*`([^`]*)`/gs;
const INTERPOLATION = /\$\{([^}]*)\}/g;
const ESCAPED = /^esc(Html|ape)\s*\(/;

function tsFiles(dir: string): string[] {
    const out: string[] = [];
    for (const entry of readdirSync(dir)) {
        const full = join(dir, entry);
        if (statSync(full).isDirectory()) {
            out.push(...tsFiles(full));
        } else if (entry.endsWith(".ts") && !entry.endsWith(".test.ts")) {
            out.push(full);
        }
    }
    return out;
}

function interpolations(): { expression: string; file: string }[] {
    const found: { expression: string; file: string }[] = [];
    for (const file of tsFiles(TS_ROOT)) {
        const source = readFileSync(file, "utf8");
        for (const block of source.matchAll(INNER_HTML_TEMPLATE)) {
            for (const match of (block[1] ?? "").matchAll(INTERPOLATION)) {
                found.push({ expression: (match[1] ?? "").trim(), file: file.slice(TS_ROOT.length + 1) });
            }
        }
    }
    return found;
}

describe("innerHTML interpolations are escaped", () => {
    test("every interpolated value is escaped or reviewed", () => {
        const unreviewed = interpolations()
            .filter(({ expression }) => !ESCAPED.test(expression) && !REVIEWED_SAFE.has(expression))
            .map(({ expression, file }) => `${file}: \${${expression}}`);

        expect([...new Set(unreviewed)].sort()).toEqual([]);
    });

    test("the scan actually finds interpolations", () => {
        // Both assertions above pass trivially if the regex stops matching.
        expect(interpolations().length).toBeGreaterThan(20);
    });

    test("the allowlist has no stale entries", () => {
        // An entry that no longer appears means the code moved on and the
        // exemption is now unexamined cover for whatever replaces it.
        const present = new Set(interpolations().map((i) => i.expression));
        const stale = [...REVIEWED_SAFE.keys()].filter((expression) => !present.has(expression));

        expect(stale).toEqual([]);
    });
});
