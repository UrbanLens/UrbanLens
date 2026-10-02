/**
 * The entry boots a live page on import, so its wiring is checked as text; shared/sortable-order.test.ts covers the
 * behaviour it wires in.
 */
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const source = readFileSync(join(import.meta.dir, "pin-list-detail.ts"), "utf8");

describe("the pin list's drag-reorder", () => {
    test("saves through orderSaveHandlers, so a failed save puts the old order back", () => {
        expect(source).toMatch(/new Sortable\(list, \{[^}]*\.\.\.orderSaveHandlers\(list, /);
    });
});
