/**
 * Guards the contract between hotkeys.ts's DEFAULT_HOTKEYS and the Settings >
 * Shortcuts section's own copy of it.
 *
 * That section has no bundled TS entry point to import DEFAULT_HOTKEYS from
 * (see settings/index.html), so its HOTKEY_DEFAULTS object duplicates the
 * action ids/labels/descriptions/default keys by hand instead - the same
 * cross-language tradeoff pin-cache.contract.test.ts already guards for
 * PIN_CACHE_VERSION. This test parses the template and fails the build if the
 * two drift, rather than letting the settings page quietly go stale.
 */

import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { DEFAULT_HOTKEYS } from "./hotkeys";

const SETTINGS_TEMPLATE = join(import.meta.dir, "../../../templates/dashboard/pages/settings/index.html");
const template = readFileSync(SETTINGS_TEMPLATE, "utf8");

describe("Settings > Shortcuts contract with hotkeys.ts", () => {
    test("the template is where we think it is", () => {
        expect(template).toContain("HOTKEY_DEFAULTS");
    });

    test("every action in DEFAULT_HOTKEYS has a matching entry in the template's copy", () => {
        const block = template.match(/var HOTKEY_DEFAULTS = \{([\s\S]*?)\n {4}\};/)?.[1];
        expect(block).toBeDefined();

        for (const [actionId, def] of Object.entries(DEFAULT_HOTKEYS)) {
            const entry = block?.match(new RegExp(`${actionId}:\\s*\\{([\\s\\S]*?)\\}`))?.[1];
            if (entry === undefined) {
                throw new Error(`${actionId} missing from the template's HOTKEY_DEFAULTS`);
            }

            const keys = [...(entry ?? "").matchAll(/'([^']+)'/g)].map((m) => m[1]);
            // keys, then label, then description are each single-quoted in turn -
            // the first len(def.keys) matches are the keys array, the next two
            // are label/description.
            expect(keys.slice(0, def.keys.length)).toEqual(def.keys);
            expect(keys[def.keys.length]).toBe(def.label);
            expect(keys[def.keys.length + 1]).toBe(def.description);
        }
    });

    test("the template has no extra actions DEFAULT_HOTKEYS doesn't know about", () => {
        const block = template.match(/var HOTKEY_DEFAULTS = \{([\s\S]*?)\n {4}\};/)?.[1] ?? "";
        const templateActionIds = [...block.matchAll(/^\s*(\w+):\s*\{/gm)].map((m) => m[1]);
        expect(templateActionIds.sort()).toEqual(Object.keys(DEFAULT_HOTKEYS).sort());
    });
});
