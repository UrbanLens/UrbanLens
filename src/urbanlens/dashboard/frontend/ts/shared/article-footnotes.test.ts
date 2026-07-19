import { describe, expect, test } from "bun:test";
import { nextReferenceNumber, referenceDefinitionStub } from "./article-footnotes";

describe("nextReferenceNumber", () => {
    test("returns 1 for content with no existing references", () => {
        expect(nextReferenceNumber("Just plain text.")).toBe(1);
    });

    test("returns one past the highest existing reference number", () => {
        expect(nextReferenceNumber("Fact.[^1] Another.[^2]\n\n[^1]: a\n[^2]: b")).toBe(3);
    });

    test("ignores reference numbers out of order in the text", () => {
        expect(nextReferenceNumber("[^3] then [^1] then [^2]")).toBe(4);
    });

    test("is unaffected by duplicate reference numbers", () => {
        expect(nextReferenceNumber("[^1] and again [^1]")).toBe(2);
    });
});

describe("referenceDefinitionStub", () => {
    test("adds a leading blank line when content does not already end with one", () => {
        expect(referenceDefinitionStub(1, "Some text")).toBe("\n\n[^1]: ");
    });

    test("does not double up the newline when content already ends with one", () => {
        expect(referenceDefinitionStub(2, "Some text\n")).toBe("\n[^2]: ");
    });

    test("handles empty content", () => {
        expect(referenceDefinitionStub(1, "")).toBe("\n\n[^1]: ");
    });
});
