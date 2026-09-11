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

    /**
     * The version and key are not the whole contract. The writer runs each pin
     * through `_slimPin`, which keeps only the fields named in `_CACHE_FIELDS`,
     * so that Set decides what this reader can actually see. Dropping or renaming
     * an entry there degrades the reader silently, exactly like the version drift
     * above: `readCachedPinsForSearch` skips every pin missing `name`/`latitude`/
     * `longitude` and returns [], so instant search suggestions just stop
     * appearing, and `readCachedPinLocations` returning [] makes the Tools-page
     * folder scanner stop filtering locations the user already has pins for.
     * Neither throws.
     */
    test("the writer still caches every field this reader consumes", () => {
        const captured = template.match(/_CACHE_FIELDS\s*=\s*new Set\(\[([\s\S]*?)\]\)/)?.[1];
        expect(captured).toBeDefined();

        const cachedFields = new Set([...(captured ?? "").matchAll(/['"]([\w]+)['"]/g)].map((m) => m[1]));
        expect(cachedFields.size).toBeGreaterThan(5); // guards against a regex that matched nothing useful

        // Every field read by readRawCachedPins' consumers in pin-cache.ts.
        for (const field of ["uuid", "name", "latitude", "longitude", "icon", "address", "label_ids"]) {
            expect(cachedFields).toContain(field);
        }
    });

    /**
     * `_CACHE_FIELDS` covers what is kept per pin. The label dictionary sits
     * beside the pins at the top of the blob instead, because a pin names its
     * labels by id and nothing can resolve those without it -
     * `readCachedPinsForSearch` would go back to returning no tags at all, which
     * is exactly how it failed before and why nobody noticed.
     */
    test("the writer still stores the label dictionary the reader resolves ids against", () => {
        const written = template.match(/localStorage\.setItem\(_CACHE_KEY[\s\S]{0,80}/);
        expect(written, "the writer's setItem call moved").not.toBeNull();

        const payload = template.match(/const payload = JSON\.stringify\(\{([\s\S]*?)\}\);/)?.[1];
        expect(payload, "could not find the cache payload literal").toBeDefined();
        expect(payload).toContain("labels:");
        expect(payload).toContain("pins:");
    });
});
