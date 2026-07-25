/**
 * isSimpleGroups() decides whether a parsed label formula can be shown as the
 * interactive 2-column Include/Exclude UI (createFilterPicker's rebuild(),
 * label-picker.ts) or must fall back to the read-only formula pill display.
 * It must say "simple" only when the 2-column rendering is *exactly*
 * equivalent to what `apply_label_groups` (dashboard/models/pin/queryset.py)
 * applies server-side - any group shape it can't represent has to route to
 * the pill display instead of silently showing a misleading simplification.
 */
import { describe, expect, test } from "bun:test";
import { isSimpleGroups, type LabelGroup } from "./label-picker";

describe("isSimpleGroups", () => {
    test("null/empty groups are simple (nothing selected)", () => {
        expect(isSimpleGroups(null)).toBe(true);
        expect(isSimpleGroups([])).toBe(true);
    });

    test("a single AND group is simple", () => {
        const groups: LabelGroup[] = [{ op: "and", ids: [1, 2, 3] }];
        expect(isSimpleGroups(groups)).toBe(true);
    });

    test("multiple AND groups collapse losslessly into flat AND - simple", () => {
        const groups: LabelGroup[] = [
            { op: "and", ids: [1, 2] },
            { op: "and", ids: [3] },
        ];
        expect(isSimpleGroups(groups)).toBe(true);
    });

    test("a single OR group is simple", () => {
        const groups: LabelGroup[] = [{ op: "or", ids: [1, 2] }];
        expect(isSimpleGroups(groups)).toBe(true);
    });

    test("any number of NOT groups is simple - exclude always flattens", () => {
        expect(isSimpleGroups([{ op: "not", ids: [1] }])).toBe(true);
        expect(
            isSimpleGroups([
                { op: "not", ids: [1] },
                { op: "not", ids: [2, 3] },
            ]),
        ).toBe(true);
    });

    test("AND + NOT together is simple (coffee AND wifi AND NOT tourist-heavy)", () => {
        const groups: LabelGroup[] = [
            { op: "and", ids: [1, 2] },
            { op: "not", ids: [3] },
        ];
        expect(isSimpleGroups(groups)).toBe(true);
    });

    test("a single OR group plus NOT is simple ((A OR B) AND NOT C)", () => {
        const groups: LabelGroup[] = [
            { op: "or", ids: [1, 2] },
            { op: "not", ids: [3] },
        ];
        expect(isSimpleGroups(groups)).toBe(true);
    });

    test("AND mixed with OR is NOT simple - 2-column can't express A AND (B OR C)", () => {
        const groups: LabelGroup[] = [
            { op: "and", ids: [1] },
            { op: "or", ids: [2, 3] },
        ];
        expect(isSimpleGroups(groups)).toBe(false);
    });

    test("two separate OR groups is NOT simple - (A OR B) AND (C OR D)", () => {
        const groups: LabelGroup[] = [
            { op: "or", ids: [1, 2] },
            { op: "or", ids: [3, 4] },
        ];
        expect(isSimpleGroups(groups)).toBe(false);
    });
});
