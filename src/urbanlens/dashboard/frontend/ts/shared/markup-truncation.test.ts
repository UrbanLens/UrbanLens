/**
 * A capped markup listing has to say it was capped.
 *
 * `MarkupJsonView` and the safety contact route keep the newest
 * `MARKUP_MAX_ITEMS_PER_RESPONSE` and report `truncated`, reading one row past
 * the ceiling so the flag costs no COUNT - which is also why there is no total
 * to show. Nothing in the browser read the flag, so a cut map rendered exactly
 * like a complete one.
 *
 * The DOM half goes through `reportMarkupTruncation` with response-shaped
 * payloads, because that is the call both readers make.
 */
import { describe, expect, test } from "bun:test";
import { markupTruncationNotice, reportMarkupTruncation } from "./markup-engine";

const NOTE = ".markup-truncation-note";

function listing(count: number, truncated: unknown): { markup_items: unknown[]; truncated: unknown } {
    return { markup_items: Array.from({ length: count }, (_, index) => ({ uuid: String(index) })), truncated };
}

describe("markupTruncationNotice", () => {
    test("says nothing when the listing was complete", () => {
        expect(markupTruncationNotice(12, false)).toBeNull();
    });

    test("says nothing when the response carries no flag at all", () => {
        expect(markupTruncationNotice(12, undefined)).toBeNull();
    });

    test("only a real boolean raises it, so a stray truthy value cannot", () => {
        expect(markupTruncationNotice(12, "true")).toBeNull();
        expect(markupTruncationNotice(12, 1)).toBeNull();
    });

    test("names how many are shown, and that they are the newest", () => {
        expect(markupTruncationNotice(500, true)).toContain("500 most recent drawings");
    });

    test("uses the singular for one", () => {
        expect(markupTruncationNotice(1, true)).toContain("1 most recent drawing.");
    });

    test("does not claim a total the server never sent", () => {
        expect(markupTruncationNotice(500, true)).not.toContain(" of ");
    });
});

describe("reportMarkupTruncation", () => {
    test("a cut listing leaves one note naming what was shown", () => {
        const host = document.createElement("div");

        reportMarkupTruncation(host, listing(5, true));

        const notes = host.querySelectorAll(NOTE);
        expect(notes.length).toBe(1);
        expect(notes[0]!.textContent).toContain("5 most recent drawings");
    });

    test("a complete listing leaves no note", () => {
        const host = document.createElement("div");

        reportMarkupTruncation(host, listing(5, false));

        expect(host.querySelector(NOTE)).toBeNull();
    });

    test("reloading a cut listing replaces the note rather than stacking another", () => {
        const host = document.createElement("div");

        reportMarkupTruncation(host, listing(5, true));
        reportMarkupTruncation(host, listing(6, true));

        const notes = host.querySelectorAll(NOTE);
        expect(notes.length).toBe(1);
        expect(notes[0]!.textContent).toContain("6 most recent drawings");
    });

    test("a reload that is no longer cut clears the note", () => {
        const host = document.createElement("div");

        reportMarkupTruncation(host, listing(5, true));
        reportMarkupTruncation(host, listing(3, false));

        expect(host.querySelector(NOTE)).toBeNull();
    });

    test("an older response without the flag is treated as complete", () => {
        const host = document.createElement("div");

        reportMarkupTruncation(host, { markup_items: [{ uuid: "a" }] });

        expect(host.querySelector(NOTE)).toBeNull();
    });

    test("is announced without taking focus", () => {
        const host = document.createElement("div");

        reportMarkupTruncation(host, listing(5, true));

        expect(host.querySelector(NOTE)!.getAttribute("role")).toBe("status");
    });

    test("leaves the wrapper's other children alone", () => {
        const host = document.createElement("div");
        const tip = document.createElement("div");
        tip.className = "markup-line-finish-tip";
        host.appendChild(tip);

        reportMarkupTruncation(host, listing(5, true));
        reportMarkupTruncation(host, listing(5, false));

        expect(host.contains(tip)).toBe(true);
    });

    test("does nothing on a page with no map wrapper", () => {
        expect(() => reportMarkupTruncation(null, listing(5, true))).not.toThrow();
    });
});
