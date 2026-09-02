import { afterEach, describe, expect, test } from "bun:test";

import { clearDismissalRingForTests, getRecentDismissals, installGlobalDismissalRing, pushDismissal } from "./dismissal-ring";

afterEach(() => {
    clearDismissalRingForTests();
    delete (window as { ulDismissalRing?: unknown }).ulDismissalRing;
});

describe("the dismissal ring", () => {
    test("pushing an explainer dismissal makes it the most recent entry", () => {
        pushDismissal("explainer", "organize-labels-intro", "Labels", "Tag your pins.");

        const [entry] = getRecentDismissals();
        expect(entry).toEqual({ id: "organize-labels-intro", kind: "explainer", heading: "Labels", body: "Tag your pins.", page: window.location.pathname });
    });

    test("a tour dismissal carries its prefix", () => {
        pushDismissal("tour", "drag-priority", "Reorder", "Drag to prioritize.", "ul_onboarding_v1_organize");

        expect(getRecentDismissals()[0]?.prefix).toBe("ul_onboarding_v1_organize");
    });

    test("heading and body are truncated", () => {
        pushDismissal("explainer", "long", "H".repeat(200), "B".repeat(1000));

        const [entry] = getRecentDismissals();
        expect(entry?.heading.length).toBe(120);
        expect(entry?.body.length).toBe(600);
    });

    test("the ring is capped at 5, newest first", () => {
        for (let i = 0; i < 7; i += 1) {
            pushDismissal("explainer", `id-${i}`, `Heading ${i}`, "");
        }

        const ring = getRecentDismissals();
        expect(ring.length).toBe(5);
        expect(ring.map((entry) => entry.id)).toEqual(["id-6", "id-5", "id-4", "id-3", "id-2"]);
    });

    test("re-dismissing the same id moves it to the front instead of duplicating", () => {
        pushDismissal("explainer", "a", "A", "");
        pushDismissal("explainer", "b", "B", "");
        pushDismissal("explainer", "a", "A again", "");

        const ring = getRecentDismissals();
        expect(ring.length).toBe(2);
        expect(ring[0]).toMatchObject({ id: "a", heading: "A again" });
    });

    test("a malformed ring in storage is treated as empty, not a throw", () => {
        sessionStorage.setItem("ul_explainer_recent", "not json");

        expect(getRecentDismissals()).toEqual([]);
    });

    test("installGlobalDismissalRing exposes push/list on window", () => {
        installGlobalDismissalRing();

        window.ulDismissalRing?.push("explainer", "x", "X", "");

        expect(window.ulDismissalRing?.list()).toEqual([{ id: "x", kind: "explainer", heading: "X", body: "", page: window.location.pathname }]);
    });
});
