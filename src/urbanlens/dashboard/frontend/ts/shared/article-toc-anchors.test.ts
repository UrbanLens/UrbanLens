import { describe, expect, test } from "bun:test";
import { anchorSlug } from "./article-toc-anchors";

describe("anchorSlug", () => {
    test("lowercases and dashes a simple title", () => {
        expect(anchorSlug("19th century", new Set())).toBe("19th-century");
    });

    test("strips punctuation", () => {
        expect(anchorSlug("Spending, Controversies & Delays", new Set())).toBe("spending-controversies-delays");
    });

    test("collapses runs of whitespace, underscores, and dashes into one dash", () => {
        expect(anchorSlug("foo   bar_baz--qux", new Set())).toBe("foo-bar-baz-qux");
    });

    test("strips leading and trailing dashes", () => {
        expect(anchorSlug("-Fires-", new Set())).toBe("fires");
    });

    test("falls back to 'section' when nothing survives stripping", () => {
        expect(anchorSlug("!!!", new Set())).toBe("section");
    });

    test("de-duplicates against already-used anchors, mutating the set", () => {
        const used = new Set<string>(["history"]);
        expect(anchorSlug("History", used)).toBe("history-2");
        expect(used.has("history-2")).toBe(true);
        expect(anchorSlug("History", used)).toBe("history-3");
    });
});
