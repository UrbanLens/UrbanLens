/**
 * Guards the contract between this reader and its only writer.
 */

import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { pinCacheKey } from "./pin-cache";

const WRITER = join(import.meta.dir, "../entries/map-page.ts");
const writer = readFileSync(WRITER, "utf8");

describe("pin cache contract with the map page's writer", () => {
    test("the writer is where we think it is", () => {
        expect(writer).toContain("_CACHE_KEY");
    });

    // Unlike the pre-migration classic script (which could not import a shared
    // module and so had to spell the version out as a literal on each side of
    // the round trip - see git history), the writer now imports PIN_CACHE_VERSION
    // directly. There is no separate literal left to drift, but a regression
    // back to a hardcoded number would still be a real bug, so guard against it.
    test("the writer reads and validates against the imported PIN_CACHE_VERSION, not a literal", () => {
        expect(writer).toContain('import { PIN_CACHE_VERSION, pinCacheKey, purgeForeignPinCaches } from "../shared/pin-cache"');
        expect(writer).toContain("v: PIN_CACHE_VERSION");
        expect(writer).toContain("c.v !== PIN_CACHE_VERSION");
    });

    test("the writer's cache key matches pinCacheKey", () => {
        const captured = writer.match(/_CACHE_KEY\s*=\s*pinCacheKey\(([^)]+)\)/)?.[1];
        expect(captured).toBe("_PROFILE_UUID");
        expect(pinCacheKey("PROFILE_UUID_HERE")).toMatch(/PROFILE_UUID_HERE/);
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
        const captured = writer.match(/_CACHE_FIELDS\s*=\s*new Set\(\[([\s\S]*?)\]\)/)?.[1];
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
        const written = writer.match(/localStorage\.setItem\(_CACHE_KEY[\s\S]{0,80}/);
        expect(written, "the writer's setItem call moved").not.toBeNull();

        const payload = writer.match(/const payload = JSON\.stringify\(\{([\s\S]*?)\}\);/)?.[1];
        expect(payload, "could not find the cache payload literal").toBeDefined();
        expect(payload).toContain("labels:");
        expect(payload).toContain("pins:");
    });
});
