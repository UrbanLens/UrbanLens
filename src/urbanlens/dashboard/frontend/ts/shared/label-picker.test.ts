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
import { isSimpleGroups, matchesLabelFilter, type LabelGroup } from "./label-picker"
import { safeColor } from "./color-safety";

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

/**
 * matchesLabelFilter() is the combined kind-tab + search-text predicate shared
 * by createFilterPicker's availability list and createChipPicker's suggestion
 * list - every label picker's "All/Tags/Categories/Statuses" tabs plus a
 * search box reduce to this one pure function.
 */
describe("matchesLabelFilter", () => {
    test("empty kind ('All') and empty query matches everything", () => {
        expect(matchesLabelFilter("tag", "Rooftop", "", "")).toBe(true);
        expect(matchesLabelFilter("category", "Water Tower", "", "")).toBe(true);
        expect(matchesLabelFilter(undefined, "Anything", "", "")).toBe(true);
    });

    test("active kind excludes labels of a different kind", () => {
        expect(matchesLabelFilter("tag", "Rooftop", "category", "")).toBe(false);
        expect(matchesLabelFilter("category", "Rooftop", "category", "")).toBe(true);
    });

    test("a label with no kind never matches a specific kind tab", () => {
        expect(matchesLabelFilter(undefined, "Rooftop", "tag", "")).toBe(false);
    });

    test("search text matches case-insensitively, anywhere in the name", () => {
        expect(matchesLabelFilter("tag", "Water Tower", "", "tower")).toBe(true);
        expect(matchesLabelFilter("tag", "Water Tower", "", "WATER")).toBe(true);
        expect(matchesLabelFilter("tag", "Water Tower", "", "silo")).toBe(false);
    });

    test("surrounding whitespace in the query is ignored", () => {
        expect(matchesLabelFilter("tag", "Rooftop", "", "  roof  ")).toBe(true);
    });

    test("kind and search combine with AND, not either/or", () => {
        // Right kind, wrong text - hidden.
        expect(matchesLabelFilter("status", "Visited", "status", "demolished")).toBe(false);
        // Right text, wrong kind - hidden.
        expect(matchesLabelFilter("tag", "Demolished", "status", "demolished")).toBe(false);
        // Both right - shown.
        expect(matchesLabelFilter("status", "Demolished", "status", "demolished")).toBe(true);
    });
});

describe("safeColor", () => {
    // Label colours reach this module through `dataset`, which returns the *decoded*
    // attribute value - so Django's escaping of the attribute does not survive the
    // round trip, and a stored colour with a quote in it arrives intact and would
    // break out of the `style="…"` the chip/pill builders construct.
    test("passes through the hex colours the server actually stores", () => {
        expect(safeColor("#2196F3")).toBe("#2196F3");
        expect(safeColor("#abc")).toBe("#abc");
        expect(safeColor("#2196f3")).toBe("#2196f3");
    });

    test("rejects a value that would break out of the style attribute", () => {
        expect(safeColor('x" onmouseover="alert(1)')).toBe("");
        expect(safeColor('#fff" onload="alert(1)')).toBe("");
    });

    test("rejects CSS injection that escaping alone would still allow", () => {
        expect(safeColor("url(https://example.com/x)")).toBe("");
        expect(safeColor("red;background:url(x)")).toBe("");
    });

    test("treats empty and missing as no colour", () => {
        expect(safeColor("")).toBe("");
        expect(safeColor(null)).toBe("");
        expect(safeColor(undefined)).toBe("");
    });
});
