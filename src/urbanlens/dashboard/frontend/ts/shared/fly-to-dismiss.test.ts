/**
 * What can honestly be asserted about the fly-to-corner dismissal here.
 *
 * The animation itself cannot: happy-dom reports `getBoundingClientRect` and
 * `getComputedStyle` as zeros, so the computed offsets are meaningless under test
 * and asserting on them would be asserting on the stub. These cover the parts that
 * are real - that the element is always removed exactly once, by whichever of the
 * two paths gets there first - which is the behaviour that matters: a card that is
 * never removed stays on screen forever, and one removed twice throws.
 */

import { beforeEach, describe, expect, test } from "bun:test";

import { flyToToolsFab, installGlobalFlyToDismiss } from "./fly-to-dismiss";

function card(): HTMLElement {
    document.body.innerHTML = '<div id="card">empty panel</div>';
    return document.getElementById("card")!;
}

beforeEach(() => {
    document.body.innerHTML = "";
});

describe("flyToToolsFab", () => {
    test("a null element is tolerated", () => {
        expect(() => flyToToolsFab(null)).not.toThrow();
    });

    test("an element already detached is removed without animating", () => {
        const el = document.createElement("div");
        expect(el.isConnected).toBe(false);
        flyToToolsFab(el);
        expect(el.classList.contains("ext-panel-dismissing")).toBe(false);
    });

    test("a connected element is taken out of flow and marked as dismissing", () => {
        const el = card();
        flyToToolsFab(el);

        expect(el.style.position).toBe("fixed");
        expect(el.style.pointerEvents).toBe("none");
        expect(el.classList.contains("ext-panel-dismissing")).toBe(true);
        expect(el.isConnected).toBe(true); // still on screen until the transition ends
    });

    test("transitionend removes it", () => {
        const el = card();
        flyToToolsFab(el);

        el.dispatchEvent(new Event("transitionend"));
        expect(el.isConnected).toBe(false);
    });

    test("the timeout removes it when transitionend never fires", async () => {
        // prefers-reduced-motion drops the transform transition, so transitionend
        // never arrives and nothing else would ever remove the card.
        const el = card();
        flyToToolsFab(el);

        await new Promise((resolve) => setTimeout(resolve, 500));
        expect(el.isConnected).toBe(false);
    });

    test("both paths firing removes it exactly once", async () => {
        const el = card();
        let removals = 0;
        const realRemove = el.remove.bind(el);
        el.remove = () => {
            removals += 1;
            realRemove();
        };

        flyToToolsFab(el);
        el.dispatchEvent(new Event("transitionend"));
        await new Promise((resolve) => setTimeout(resolve, 500));

        expect(removals).toBe(1);
    });
});

describe("installGlobalFlyToDismiss", () => {
    test("exposes the global the empty-card templates call", () => {
        installGlobalFlyToDismiss();
        expect(typeof window.ulFlyToToolsFab).toBe("function");
    });
});
