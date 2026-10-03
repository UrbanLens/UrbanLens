import { describe, expect, test } from "bun:test";
import { departureBadge, departureLabel, rankFinalScoreboard } from "./game-scoreboard";

describe("departureLabel", () => {
    test("names a player who left", () => {
        expect(departureLabel("left")).toBe("Left");
    });

    test("names a player the host removed", () => {
        expect(departureLabel("removed")).toBe("Removed");
    });

    test("is null for a player who finished", () => {
        expect(departureLabel(null)).toBeNull();
        expect(departureLabel(undefined)).toBeNull();
    });
});

describe("departureBadge", () => {
    test("renders the label as a neutral badge", () => {
        const badge = departureBadge("removed");
        expect(badge?.textContent).toBe("Removed");
        expect(badge?.classList.contains("ul-badge")).toBe(true);
        expect(badge?.classList.contains("ul-badge--neutral")).toBe(true);
    });

    test("renders nothing for a player who finished", () => {
        expect(departureBadge(null)).toBeNull();
    });
});

describe("rankFinalScoreboard", () => {
    const finisherLow = { name: "low", points: 100, departure: null };
    const finisherHigh = { name: "high", points: 500 };
    const leftRich = { name: "left-rich", points: 9000, departure: "left" as const };
    const removedPoor = { name: "removed-poor", points: 10, departure: "removed" as const };

    test("ranks finishers by points", () => {
        const ranked = rankFinalScoreboard([finisherLow, finisherHigh]);
        expect(ranked.map(({ entry, rank }) => [entry.name, rank])).toEqual([
            ["high", 1],
            ["low", 2],
        ]);
    });

    test("puts departed players after every finisher, unranked, whatever their points", () => {
        const ranked = rankFinalScoreboard([leftRich, finisherLow, removedPoor, finisherHigh]);
        expect(ranked.map(({ entry, rank }) => [entry.name, rank])).toEqual([
            ["high", 1],
            ["low", 2],
            ["left-rich", null],
            ["removed-poor", null],
        ]);
    });

    test("orders departed players by points among themselves", () => {
        const ranked = rankFinalScoreboard([removedPoor, leftRich]);
        expect(ranked.map(({ entry }) => entry.name)).toEqual(["left-rich", "removed-poor"]);
    });

    test("does not reorder its input", () => {
        const input = [finisherLow, finisherHigh];
        rankFinalScoreboard(input);
        expect(input).toEqual([finisherLow, finisherHigh]);
    });
});
