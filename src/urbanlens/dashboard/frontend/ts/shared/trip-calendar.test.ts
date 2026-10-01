import { describe, expect, test } from "bun:test";

import { activitiesForTab, calendarHtml, tripMonths, type CalendarActivity } from "./trip-calendar";

const TODAY = new Date(2026, 8, 29);

function activity(overrides: Partial<CalendarActivity> = {}): CalendarActivity {
    return { id: "7", date: "2026-10-03", title: "Mill", index: "1", status: "confirmed", moveUrl: "/trips/t/activities/7/move/", ...overrides };
}

describe("tripMonths", () => {
    test("spans the trip's months, crossing a year", () => {
        expect(tripMonths("2026-11-20", "2027-02-02", 0, TODAY)).toEqual([
            { y: 2026, m: 10 },
            { y: 2026, m: 11 },
            { y: 2027, m: 0 },
            { y: 2027, m: 1 },
        ]);
    });

    test("an undated trip shows the current month, and paging shifts it", () => {
        expect(tripMonths("", "", 0, TODAY)).toEqual([{ y: 2026, m: 8 }]);
        expect(tripMonths("", "", -9, TODAY)).toEqual([{ y: 2025, m: 11 }]);
    });

    test("a very long trip is capped", () => {
        expect(tripMonths("2020-01-01", "2030-01-01", 0, TODAY)).toHaveLength(18);
    });
});

describe("activitiesForTab", () => {
    const all = [activity({ id: "1", status: "proposed" }), activity({ id: "2", status: "confirmed" }), activity({ id: "3", status: "completed" })];

    test("upcoming shows everything not completed", () => {
        expect(activitiesForTab(all, "upcoming").map((a) => a.id)).toEqual(["1", "2"]);
    });

    test("completed shows only completed, and a status tab only its own", () => {
        expect(activitiesForTab(all, "completed").map((a) => a.id)).toEqual(["3"]);
        expect(activitiesForTab(all, "proposed").map((a) => a.id)).toEqual(["1"]);
    });
});

describe("calendarHtml", () => {
    function render(activities: CalendarActivity[]): HTMLElement {
        const host = document.createElement("div");
        host.innerHTML = calendarHtml(tripMonths("2026-10-01", "2026-10-31", 0, TODAY), activities, TODAY);
        return host;
    }

    test("puts each activity on its day, draggable, with its move URL", () => {
        const host = render([activity()]);
        const event = host.querySelector<HTMLElement>('.cal-cell[data-date="2026-10-03"] .cal-event');
        expect(event?.dataset.actId).toBe("7");
        expect(event?.dataset.moveUrl).toBe("/trips/t/activities/7/move/");
        expect(event?.querySelector(".cal-event-num")?.textContent).toBe("1");
    });

    test("an activity title is text, in the cell and its tooltip", () => {
        const title = '<img id="cal-canary" src=x>" onmouseover="x';
        const host = render([activity({ title })]);
        expect(host.querySelector("#cal-canary")).toBeNull();
        expect(host.querySelector(".cal-event")?.getAttribute("title")).toBe(title);
        expect(host.querySelector(".cal-event")?.getAttribute("onmouseover")).toBeNull();
    });

    test("the paging buttons carry their direction for the page's delegated handler", () => {
        const deltas = [...render([]).querySelectorAll<HTMLElement>('[data-trip-action="cal-nav"]')].map((b) => b.dataset.delta);
        expect(deltas).toEqual(["-1", "1"]);
    });

    test("October 2026 starts on a Thursday", () => {
        expect(render([]).querySelectorAll(".cal-cell--empty")).toHaveLength(4);
    });
});
