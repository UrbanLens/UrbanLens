/**
 * The Organize page speaks two vocabularies for the same three things.
 *
 * `Label.kind` - what `data-kind` carries in rendered markup - is
 * `"tag" | "category" | "status"` (see `models/labels/meta.py`). `OrgNamespace`,
 * which every per-namespace registry on `window` is keyed by, abbreviates the
 * middle one to `"cat"`.
 *
 * Two of three values coincide, which is what made this expensive: the Display
 * Order tab looked up `window._orgBulkEditByIds[kind]` (and, once added, the
 * merge and delete registries) straight from `data-kind`, so tags and statuses
 * worked and categories alone fell through to "not available for this type".
 * A bug that only affects the middle of three sibling cases reads as a backend
 * permissions problem, not a string mismatch.
 *
 * These tests pin the translation and, more importantly, pin that every kind
 * the priority list can render has one.
 */

import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { ORG_FILTER_NAMESPACES, ORG_NS_BY_LABEL_KIND } from "./organize-filter-engine";

const META = join(import.meta.dir, "../../../models/labels/meta.py");
const PRIORITY = join(import.meta.dir, "./organize-priority.ts");

/** The `KIND_* = "..."` values Python defines, which is what `data-kind` renders. */
function labelKindsFromPython(): string[] {
    const source = readFileSync(META, "utf8");
    return [...source.matchAll(/^KIND_(\w+)\s*=\s*"([^"]+)"/gm)].map((m) => m[2]!);
}

describe("Label.kind to OrgNamespace translation", () => {
    test("the python side is where we think it is", () => {
        expect(labelKindsFromPython().length).toBeGreaterThanOrEqual(3);
    });

    test("category translates rather than passing through", () => {
        expect(ORG_NS_BY_LABEL_KIND["category"]).toBe("cat");
    });

    test("the coincidentally-equal kinds still map to themselves", () => {
        expect(ORG_NS_BY_LABEL_KIND["tag"]).toBe("tag");
        expect(ORG_NS_BY_LABEL_KIND["status"]).toBe("status");
    });

    test("every mapped value is a real namespace", () => {
        for (const ns of Object.values(ORG_NS_BY_LABEL_KIND)) {
            expect(ORG_FILTER_NAMESPACES).toContain(ns);
        }
    });

    test("every label kind the priority list can show has a translation", () => {
        // The priority list excludes only KIND_USER and KIND_MEDIA
        // (`_NON_PRIORITY_KINDS` in controllers/organize.py); everything else can
        // appear there and therefore needs a registry lookup that resolves.
        const shown = labelKindsFromPython().filter((kind) => kind !== "user" && kind !== "media");

        for (const kind of shown) {
            expect(ORG_NS_BY_LABEL_KIND[kind]).toBeDefined();
        }
    });

    test("the bulk registries are looked up through the translation, not raw", () => {
        // Cheap structural guard: a future edit that goes back to
        // `window._orgBulkFooByIds[kind]` reintroduces the categories-only bug,
        // and nothing else in the suite would notice.
        const source = readFileSync(PRIORITY, "utf8");
        const rawLookups = [...source.matchAll(/window\._orgBulk\w+ByIds\[([^\]]+)\]/g)].map((m) => m[1]!);

        expect(rawLookups.length).toBeGreaterThan(0);
        for (const expression of rawLookups) {
            expect(expression).toContain("ORG_NS_BY_LABEL_KIND");
        }
    });
});
