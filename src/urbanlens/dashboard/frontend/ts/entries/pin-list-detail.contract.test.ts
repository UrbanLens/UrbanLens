/**
 * The entry boots a live page on import, so its wiring is checked as text; shared/sortable-order.test.ts covers the
 * behaviour it wires in.
 */
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const source = readFileSync(join(import.meta.dir, "pin-list-detail.ts"), "utf8");

describe("a smart list catching up on pin changes", () => {
    test("reloads its items once the sync settles, unless the reader has scrolled or is dragging", () => {
        expect(source).toMatch(/addEventListener\("pinListMembershipSettled", \(\) => this\.membershipSettled\(\)\)/);
        expect(source).toMatch(/rows > pageSize\)\) return;\s+this\.refreshItems\(\);/);
    });
});

describe("the pin list's drag-reorder", () => {
    test("saves through orderSaveHandlers, so a failed save puts the old order back", () => {
        expect(source).toMatch(/new Sortable\(list, \{[^}]*\.\.\.orderSaveHandlers\(list, /);
    });
});
