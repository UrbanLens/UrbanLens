/**
 * The eviction order is the whole point: a page over the browser's WebGL context limit loses the
 * *oldest* context, silently, and the thumbnails the reader is looking at are the newest. A budget
 * that evicted in the wrong order would blank exactly the maps the reader can see.
 */
import { describe, expect, test } from "bun:test";
import { MAX_LIVE_THUMB_MAPS, ThumbMapBudget } from "./thumb-map-budget";

/** Stands in for a thumbnail container; the budget only ever uses it as an identity. */
function element(id: string): Element {
    return { id } as unknown as Element;
}

/** A budget of `limit`, plus the ids it disposed, in order. */
function budgetOf(limit: number): { budget: ThumbMapBudget; disposed: string[]; admit: (id: string) => Element | null } {
    const disposed: string[] = [];
    const budget = new ThumbMapBudget(limit);
    const seen = new Map<string, Element>();
    const admit = (id: string): Element | null => {
        const el = seen.get(id) ?? element(id);
        seen.set(id, el);
        return budget.admit(el, (e) => disposed.push((e as { id: string }).id));
    };
    return { budget, disposed, admit };
}

describe("the budget", () => {
    test("holds everything up to its limit", () => {
        const { budget, disposed, admit } = budgetOf(3);

        ["a", "b", "c"].forEach(admit);

        expect(budget.size).toBe(3);
        expect(disposed).toEqual([]);
    });

    test("evicts the oldest to make room, and only one per admission", () => {
        const { budget, disposed, admit } = budgetOf(3);

        ["a", "b", "c", "d"].forEach(admit);

        expect(disposed).toEqual(["a"]);
        expect(budget.size).toBe(3);
    });

    /** A reader scrolling back up re-renders an earlier thumbnail; that one is now the newest. */
    test("counts a touch as use, so the recently seen outlive the merely early", () => {
        const { disposed, admit, budget } = budgetOf(3);
        const first = element("a");
        budget.admit(first, (e) => disposed.push((e as { id: string }).id));
        ["b", "c"].forEach(admit);

        budget.touch(first);
        admit("d");

        expect(disposed).toEqual(["b"]);
    });

    /** A re-render in place must not consume a second slot for the same element. */
    test("re-admitting an element it already holds does not evict anything", () => {
        const { budget, disposed, admit } = budgetOf(2);

        admit("a");
        admit("b");
        admit("a");

        expect(disposed).toEqual([]);
        expect(budget.size).toBe(2);
    });

    test("a limit of one still works rather than evicting what was just admitted", () => {
        const { budget, disposed, admit } = budgetOf(1);

        admit("a");
        admit("b");

        expect(disposed).toEqual(["a"]);
        expect(budget.size).toBe(1);
        expect(budget.holds(element("b"))).toBe(false); // a different object with the same id
    });

    test("forget drops the bookkeeping without disposing, for a map something else released", () => {
        const { budget, disposed } = budgetOf(2);
        const a = element("a");
        budget.admit(a, (e) => disposed.push((e as { id: string }).id));

        budget.forget(a);

        expect(disposed).toEqual([]);
        expect(budget.size).toBe(0);
        expect(budget.holds(a)).toBe(false);
    });

    test("evict releases a named element even when there is room to spare", () => {
        const { budget, disposed } = budgetOf(5);
        const a = element("a");
        budget.admit(a, (e) => disposed.push((e as { id: string }).id));

        expect(budget.evict(a)).toBe(a);
        expect(disposed).toEqual(["a"]);
        expect(budget.evict(a)).toBeNull();
    });

    /** A slot left counted because its disposer threw is a slot the page never gets back. */
    test("a disposer that throws still frees its slot", () => {
        const budget = new ThumbMapBudget(2);
        const a = element("a");
        budget.admit(a, () => {
            throw new Error("Leaflet's remove() is not safe to call twice");
        });

        budget.evict(a);

        expect(budget.size).toBe(0);
    });

    test("the default is under every browser's context limit, with room for a page's own maps", () => {
        // Chrome allows 16 per page and Safari fewer; a comment thread is not the only map on the
        // page, and the expanded dialog opens one more on top of whatever is already live.
        expect(MAX_LIVE_THUMB_MAPS).toBeLessThanOrEqual(8);
        expect(MAX_LIVE_THUMB_MAPS).toBeGreaterThan(1);
    });
});
