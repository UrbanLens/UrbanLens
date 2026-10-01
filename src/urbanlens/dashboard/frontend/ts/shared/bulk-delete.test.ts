import { describe, expect, test } from "bun:test";

import { bulkDeleteOutcome } from "./bulk-delete";

describe("bulkDeleteOutcome", () => {
    test("everything selected went", () => {
        expect(bulkDeleteOutcome([1, 2], { deleted: 2, unlinked: 0, image_ids: [1, 2] })).toEqual({ gone: [1, 2], message: "Deleted 2 photos." });
    });

    test("says which stayed on the wiki", () => {
        expect(bulkDeleteOutcome([1, 2], { deleted: 1, unlinked: 1, image_ids: [1, 2] }).message).toBe("Deleted 1 photo. Removed 1 photo from this pin; still on the wiki.");
    });

    test("photos the endpoint skipped stay, and the viewer is told", () => {
        expect(bulkDeleteOutcome([1, 2, 3], { deleted: 1, unlinked: 0, image_ids: [1] })).toEqual({
            gone: [1],
            message: "Deleted 1 photo. Left 2 photos that can't be deleted from here.",
        });
    });

    test("an older response without ids is taken at its word", () => {
        expect(bulkDeleteOutcome([4, 5], { deleted: 2 })).toEqual({ gone: [4, 5], message: "Deleted 2 photos." });
    });
});
