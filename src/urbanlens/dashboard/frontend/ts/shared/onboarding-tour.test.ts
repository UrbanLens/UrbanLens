import { afterEach, describe, expect, test } from "bun:test";

import { clearDismissalRingForTests, getRecentDismissals } from "./dismissal-ring";
import { initOnboardingTour, type OnboardingCard } from "./onboarding-tour";

const PREFIX = "ul_onboarding_v1_test";

function card(overrides: Partial<OnboardingCard> = {}): OnboardingCard {
    return {
        id: "step-one",
        icon: "info",
        target: "#target",
        eyebrow: "Tip",
        title: "Step one",
        body: "Do the thing.",
        button: "Got it",
        action: () => undefined,
        ready: () => true,
        ...overrides,
    };
}

afterEach(() => {
    clearDismissalRingForTests();
    document.body.innerHTML = "";
    localStorage.clear();
});

describe("the onboarding tour", () => {
    test("dismissing a card ('Don't show again') pushes it to the dismissal ring", async () => {
        document.body.innerHTML = '<div id="host"></div><div id="target"></div>';
        initOnboardingTour({ prefix: PREFIX, hostSelector: "#host", cards: [card()], initialDelayMs: 0 });

        for (let attempt = 0; attempt < 200 && !document.querySelector(".js-onboarding-dismiss"); attempt += 1) {
            await new Promise((resolve) => setTimeout(resolve, 1));
        }
        document.querySelector<HTMLButtonElement>(".js-onboarding-dismiss")?.click();

        expect(getRecentDismissals()).toEqual([{ id: "step-one", kind: "tour", heading: "Step one", body: "Do the thing.", page: window.location.pathname, prefix: PREFIX }]);
    });

    test("a matching ul:tour-restart event clears the dismissal and re-shows the card", async () => {
        document.body.innerHTML = '<div id="host"></div><div id="target"></div>';
        localStorage.setItem(`${PREFIX}_step-one_dismissed`, "1");
        initOnboardingTour({ prefix: PREFIX, hostSelector: "#host", cards: [card()], initialDelayMs: 100_000 });
        expect(document.querySelector(".page-onboarding-card")).toBeNull();

        document.dispatchEvent(new CustomEvent("ul:tour-restart", { detail: { prefix: PREFIX } }));

        expect(localStorage.getItem(`${PREFIX}_step-one_dismissed`)).toBeNull();
        expect(document.querySelector(".page-onboarding-card")).not.toBeNull();
    });

    test("ul:tour-restart for a different prefix is ignored", () => {
        document.body.innerHTML = '<div id="host"></div><div id="target"></div>';
        localStorage.setItem(`${PREFIX}_step-one_dismissed`, "1");
        initOnboardingTour({ prefix: PREFIX, hostSelector: "#host", cards: [card()], initialDelayMs: 100_000 });

        document.dispatchEvent(new CustomEvent("ul:tour-restart", { detail: { prefix: "some_other_prefix" } }));

        expect(localStorage.getItem(`${PREFIX}_step-one_dismissed`)).toBe("1");
        expect(document.querySelector(".page-onboarding-card")).toBeNull();
    });
});
