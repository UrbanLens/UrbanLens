/**
 * Guards the contract between this reader and its only writer.
 *
 * The map page's inline script writes the localStorage pin cache; pin-cache.ts
 * reads it. The cache key and payload version are spelled out independently on
 * each side, in different languages, so nothing but agreement-by-convention kept
 * them together - and that already failed once: the reader sat on v6 while the
 * writer moved on, so every read returned [] and the features built on it went
 * quiet without erroring.
 *
 * These tests parse the template and fail the build on the next such drift.
 */

import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { PIN_CACHE_VERSION, pinCacheKey } from "./pin-cache";

const MAP_TEMPLATE = join(import.meta.dir, "../../../templates/dashboard/pages/map/index.html");
const template = readFileSync(MAP_TEMPLATE, "utf8");

describe("pin cache contract with the map page's inline writer", () => {
    test("the template is where we think it is", () => {
        expect(template).toContain("_CACHE_KEY");
    });

    test("every cache-version literal in the template matches PIN_CACHE_VERSION", () => {
        // Both sides of the writer's own round trip: `v: 8` when writing,
        // `c.v !== 8` when validating what it read back.
        const written = [...template.matchAll(/\bv:\s*(\d+)\s*,/g)].map((m) => Number(m[1]));
        const validated = [...template.matchAll(/\.v\s*!==\s*(\d+)/g)].map((m) => Number(m[1]));

        expect(written.length).toBeGreaterThan(0);
        expect(validated.length).toBeGreaterThan(0);
        for (const version of [...written, ...validated]) {
            expect(version).toBe(PIN_CACHE_VERSION);
        }
    });

    test("the template's cache key matches pinCacheKey", () => {
        const captured = template.match(/_CACHE_KEY\s*=\s*`([^`]+)`/)?.[1];
        expect(captured).toBeDefined();

        // The template builds it as a JS template literal interpolating _PROFILE_UUID.
        const templateKey = (captured ?? "").replace("${_PROFILE_UUID}", "PROFILE_UUID_HERE");
        expect(templateKey).toBe(pinCacheKey("PROFILE_UUID_HERE"));
    });
});
