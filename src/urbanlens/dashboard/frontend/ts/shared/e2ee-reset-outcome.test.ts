/**
 * What a key reset tells the user about their message history.
 *
 * A reset can preserve all, some, or none of the account's encrypted history,
 * and only the server knows which - it reports `rewrapped` and `not_rewrapped`.
 * The previous inline version of this logic claimed "everything stays
 * readable" whenever *any* row had been re-encrypted, and said nothing at all
 * when the old key was held but nothing came back re-encrypted. Both are cases
 * where the user permanently loses conversations and is not told.
 *
 * See PROBLEMS.md, "the E2EE key reset could destroy preservable history".
 */

import { describe, expect, test } from "bun:test";

import { resetOutcomeMessage } from "./e2ee-client";

describe("a reset that preserved everything", () => {
    test("is a plain success", () => {
        const outcome = resetOutcomeMessage({ rewrapped: 7, notRewrapped: 0 });
        expect(outcome.level).toBe("success");
        expect(outcome.message).toContain("everything stays readable");
    });
});

describe("a reset that preserved nothing", () => {
    test("warns that history is gone", () => {
        const outcome = resetOutcomeMessage({ rewrapped: 0, notRewrapped: 4 });
        expect(outcome.level).toBe("warning");
        expect(outcome.message).toContain("no longer readable");
    });
});

describe("a partial reset", () => {
    // The case the old code got wrong: it reported unqualified success.
    test("warns rather than claiming success", () => {
        const outcome = resetOutcomeMessage({ rewrapped: 3, notRewrapped: 9 });
        expect(outcome.level).toBe("warning");
    });

    test("names both counts, so the loss is quantified", () => {
        const outcome = resetOutcomeMessage({ rewrapped: 3, notRewrapped: 9 });
        expect(outcome.message).toContain("3");
        expect(outcome.message).toContain("9");
    });

    test("never claims everything stays readable", () => {
        const outcome = resetOutcomeMessage({ rewrapped: 3, notRewrapped: 9 });
        expect(outcome.message).not.toContain("everything stays readable");
    });

    test("a single lost conversation is still a warning", () => {
        expect(resetOutcomeMessage({ rewrapped: 100, notRewrapped: 1 }).level).toBe("warning");
    });
});

describe("a reset on an account with no encrypted history", () => {
    // The other case the old code got wrong - it showed no toast at all, so a
    // reset that had worked looked like one that had silently failed.
    test("still confirms the reset happened", () => {
        const outcome = resetOutcomeMessage({ rewrapped: 0, notRewrapped: 0 });
        expect(outcome.level).toBe("success");
        expect(outcome.message).toBeTruthy();
    });
});
