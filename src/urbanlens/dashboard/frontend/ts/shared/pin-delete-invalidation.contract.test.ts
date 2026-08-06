/**
 * Guards the invariant that deleting a pin invalidates the map's pin cache.
 *
 * The map keeps its pins in localStorage and refreshes them from a poll that
 * compares `Max(updated)` across the profile's pins. A *deletion* cannot advance
 * that timestamp, so the poll is structurally blind to it - the only signal is
 * the `ul_pins_dirty` flag.
 *
 * `deletePinCascade` is shared: the map page calls it and the pin detail page
 * calls it. The map's call site set the flag itself, so the detail page's
 * deletion left the map restoring a pin that no longer exists from cache, with a
 * marker that 404s when clicked. The flag belongs in the shared helper, where
 * every present and future caller gets it, and this test pins it there.
 */

import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const BASE_TEMPLATE = join(import.meta.dir, "../../../templates/dashboard/themes/base.html");
const template = readFileSync(BASE_TEMPLATE, "utf8");

/** The body of `window.deletePinCascade = async function (...) { ... }`, brace-matched. */
function deletePinCascadeBody(source: string): string {
    const start = source.indexOf("window.deletePinCascade");
    expect(start).toBeGreaterThan(-1);
    const open = source.indexOf("{", start);
    let depth = 0;
    for (let i = open; i < source.length; i += 1) {
        if (source[i] === "{") depth += 1;
        else if (source[i] === "}") {
            depth -= 1;
            if (depth === 0) return source.slice(open, i + 1);
        }
    }
    throw new Error("unbalanced braces in deletePinCascade");
}

describe("pin deletion invalidates the map pin cache", () => {
    test("the shared helper flags the cache dirty itself", () => {
        expect(deletePinCascadeBody(template)).toContain("ul_pins_dirty");
    });

    test("it flags before returning success, not after the early bail-outs", () => {
        const body = deletePinCascadeBody(template);
        // `return false` is the user-cancelled path; nothing was deleted, so the flag
        // must not be set before the first of those.
        expect(body.indexOf("ul_pins_dirty")).toBeGreaterThan(body.indexOf("if (!confirmed) return false;"));
    });
});
