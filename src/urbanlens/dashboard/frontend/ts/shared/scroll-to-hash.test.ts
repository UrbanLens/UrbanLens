import { beforeEach, describe, expect, mock, test } from "bun:test";

import { installGlobalScrollToHash, scrollToHash } from "./scroll-to-hash";

/** Give the target an observable scrollIntoView - happy-dom's is a no-op. */
function target(id: string): ReturnType<typeof mock> {
    document.body.innerHTML = `<div id="${id}">anchor</div>`;
    const spy = mock((_opts?: ScrollIntoViewOptions) => {});
    document.getElementById(id)!.scrollIntoView = spy as unknown as Element["scrollIntoView"];
    return spy;
}

function setHash(hash: string): void {
    window.location.hash = hash;
}

function settle(): void {
    document.dispatchEvent(new Event("htmx:afterSettle"));
}

installGlobalScrollToHash();

beforeEach(() => {
    setHash("");
    document.body.innerHTML = "";
});

describe("after an HTMX swap", () => {
    test("scrolls to the anchor the url points at", () => {
        const spy = target("comment-42");
        setHash("#comment-42");
        settle();

        expect(spy).toHaveBeenCalled();
        expect((spy.mock.calls[0]?.[0] as ScrollIntoViewOptions)?.block).toBe("center");
    });

    test("does nothing when the url has no fragment", () => {
        const spy = target("comment-42");
        settle();
        expect(spy).not.toHaveBeenCalled();
    });

    test("does nothing when the anchor is not on the page", () => {
        target("comment-42");
        setHash("#comment-99");
        expect(() => settle()).not.toThrow();
    });

    test("scrolls again once the anchor arrives in a later swap", () => {
        // The whole reason this re-runs: a link to a comment lands before HTMX has
        // fetched the comment it points at.
        setHash("#comment-42");
        settle(); // nothing to find yet

        const spy = target("comment-42");
        settle();
        expect(spy).toHaveBeenCalled();
    });
});

describe("a fragment that is not a valid CSS selector", () => {
    // querySelector throws a DOMException on these. They are not hypothetical:
    // OAuth providers append "#_=_" and "#access_token=..." on redirect back, and
    // this app signs in through Google and Discord.
    //
    // Asserted against scrollToHash directly, not through dispatchEvent: an
    // exception thrown inside a listener is reported rather than propagated, so
    // wrapping the dispatch in .not.toThrow() would pass no matter what happened.
    const invalid = ["#_=_", "#access_token=abc123", "#/route", "#foo=bar", "#123", "#!", "#sec:2"];

    for (const hash of invalid) {
        test(`${hash} does not throw`, () => {
            target("comment-42");
            setHash(hash);
            expect(() => scrollToHash()).not.toThrow();
        });
    }

    test("a valid fragment still works afterwards", () => {
        setHash("#_=_");
        expect(() => scrollToHash()).not.toThrow();

        const spy = target("comment-42");
        setHash("#comment-42");
        scrollToHash();
        expect(spy).toHaveBeenCalled();
    });

    test("an id starting with a digit now scrolls rather than throwing", () => {
        // Valid HTML id, invalid CSS selector - it used to throw instead of working.
        const spy = target("123");
        setHash("#123");
        scrollToHash();
        expect(spy).toHaveBeenCalled();
    });

    test("a percent-encoded id is decoded", () => {
        const spy = target("a b");
        setHash("#a%20b");
        scrollToHash();
        expect(spy).toHaveBeenCalled();
    });

    test("a malformed percent escape is survived", () => {
        target("comment-42");
        setHash("#%E0%A4%A");
        expect(() => scrollToHash()).not.toThrow();
    });
});
