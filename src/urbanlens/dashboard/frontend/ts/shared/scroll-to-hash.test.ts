import { beforeEach, describe, expect, mock, test } from "bun:test";

import { installGlobalScrollToHash, resetScrollToHashForTests, scrollToHash } from "./scroll-to-hash";

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
    resetScrollToHashForTests();
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

    test("does not scroll again on a later, unrelated swap once it already landed", () => {
        // A page reached via a hash link keeps that hash in the URL for as long as the reader stays on it.
        const spy = target("comment-42");
        setHash("#comment-42");
        settle();
        expect(spy).toHaveBeenCalledTimes(1);

        settle();
        settle();
        expect(spy).toHaveBeenCalledTimes(1);
    });

    test("scrolls again if the hash changes to a different anchor", () => {
        target("comment-42");
        setHash("#comment-42");
        settle();

        const spy = target("comment-99");
        setHash("#comment-99");
        settle();
        expect(spy).toHaveBeenCalled();
    });
});

describe("a fragment that is not a valid CSS selector", () => {
    // querySelector throws a DOMException on these.
    //
    // Asserted against scrollToHash directly, not through dispatchEvent.
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

describe("a collapsed answer", () => {
    function details(html: string, id: string) {
        document.body.innerHTML = html;
        const spy = mock((_opts?: boolean | ScrollIntoViewOptions) => {});
        const el = document.getElementById(id);
        if (el) el.scrollIntoView = spy;
        return spy;
    }
    const isOpen = (id: string) => {
        const el = document.getElementById(id);
        return el instanceof HTMLDetailsElement && el.open;
    };

    test("a <details> the url names opens, and its question lands at the top", () => {
        const spy = details(`<details id="q"><summary>Q</summary><p>A</p></details>`, "q");
        setHash("#q");
        scrollToHash();
        expect(isOpen("q")).toBe(true);
        const opts = spy.mock.calls[0]?.[0];
        expect(typeof opts === "object" ? opts.block : undefined).toBe("start");
    });

    test("an anchor inside collapsed sections opens every one around it", () => {
        details(`<details id="outer"><summary>O</summary><details id="inner"><summary>I</summary><p id="a">A</p></details></details>`, "a");
        setHash("#a");
        scrollToHash();
        expect([isOpen("outer"), isOpen("inner")]).toEqual([true, true]);
    });

    test("following a link to another answer on the page opens that one", () => {
        const spy = details(`<details id="q1"><summary>1</summary></details><details id="q2"><summary>2</summary></details>`, "q2");
        setHash("#q2");
        window.dispatchEvent(new Event("hashchange"));
        expect(isOpen("q2")).toBe(true);
        expect(spy).toHaveBeenCalledTimes(1);
    });

    test("a link to an answer already open, or to a plain section, is left to the browser's own jump", () => {
        const spy = details(`<details id="q3" open><summary>3</summary></details><section id="s"></section>`, "q3");
        const section = mock((_opts?: boolean | ScrollIntoViewOptions) => {});
        const s = document.getElementById("s");
        if (s) s.scrollIntoView = section;
        for (const hash of ["#q3", "#s"]) {
            setHash(hash);
            window.dispatchEvent(new Event("hashchange"));
        }
        expect([spy.mock.calls.length, section.mock.calls.length]).toEqual([0, 0]);
    });
});
