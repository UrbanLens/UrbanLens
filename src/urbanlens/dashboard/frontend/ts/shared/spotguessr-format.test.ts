import { describe, expect, test } from "bun:test";
import {
    avatarInitial,
    bonusSuffix,
    countUpValue,
    easeOutQuad,
    formatCountdown,
    formatRatingDelta,
    interpolateLatLng,
    panelVisibility,
    summaryBestRoundSubtitle,
    summaryHeadline,
} from "./spotguessr-format";

describe("panelVisibility", () => {
    test("only the active panel is visible", () => {
        expect(panelVisibility("game")).toEqual({
            settings: false,
            lobby: false,
            game: true,
            summary: false,
            empty: false,
        });
    });

    test("switching the active panel switches which one is true", () => {
        expect(panelVisibility("settings")).toEqual({
            settings: true,
            lobby: false,
            game: false,
            summary: false,
            empty: false,
        });
    });
});

describe("bonusSuffix", () => {
    test("renders nothing when there's no bonus", () => {
        expect(bonusSuffix(0, [])).toBe("");
    });

    test("renders the total and matched tiers", () => {
        expect(bonusSuffix(750, ["country", "state", "city"])).toBe(" (+750 bonus: country, state, city)");
    });

    test("renders a single matched tier", () => {
        expect(bonusSuffix(250, ["country"])).toBe(" (+250 bonus: country)");
    });
});

describe("avatarInitial", () => {
    test("uppercases the first character", () => {
        expect(avatarInitial("jess")).toBe("J");
    });

    test("falls back to a question mark for an empty username", () => {
        expect(avatarInitial("")).toBe("?");
    });
});

describe("formatRatingDelta", () => {
    test("returns null for a missing delta (round not yet revealed)", () => {
        expect(formatRatingDelta(null)).toBeNull();
        expect(formatRatingDelta(undefined)).toBeNull();
    });

    test("formats a positive delta with an up arrow and a plus sign", () => {
        expect(formatRatingDelta(14.2)).toEqual({ text: "▲ +14.2 rating", direction: "up" });
    });

    test("formats a negative delta with a down arrow and no extra minus sign", () => {
        expect(formatRatingDelta(-8.6)).toEqual({ text: "▼ -8.6 rating", direction: "down" });
    });

    test("rounds to one decimal place", () => {
        expect(formatRatingDelta(3.14159)).toEqual({ text: "▲ +3.1 rating", direction: "up" });
    });

    test("treats an exact zero as flat, not up or down", () => {
        expect(formatRatingDelta(0)).toEqual({ text: "±0 rating", direction: "flat" });
    });

    test("rounds a tiny delta down to flat", () => {
        expect(formatRatingDelta(0.04)).toEqual({ text: "±0 rating", direction: "flat" });
    });
});

describe("formatCountdown", () => {
    test("renders sub-minute durations as bare seconds", () => {
        expect(formatCountdown(45)).toBe("45s");
        expect(formatCountdown(0)).toBe("0s");
    });

    test("renders minute-plus durations as m:ss", () => {
        expect(formatCountdown(90)).toBe("1:30");
        expect(formatCountdown(605)).toBe("10:05");
    });

    test("clamps a negative remaining time to zero", () => {
        expect(formatCountdown(-5)).toBe("0s");
    });
});

describe("summaryBestRoundSubtitle", () => {
    test("returns undefined when there's no best round on record", () => {
        expect(summaryBestRoundSubtitle({ profile_id: 1, username: "a", total_points: 0 })).toBeUndefined();
    });

    test("includes distance when available", () => {
        expect(
            summaryBestRoundSubtitle({ profile_id: 1, username: "a", total_points: 100, best_round_points: 4200, best_round_distance_meters: 320 }),
        ).toBe("Best round: 4200 pts (0.32 km)");
    });

    test("omits distance when it isn't available", () => {
        expect(summaryBestRoundSubtitle({ profile_id: 1, username: "a", total_points: 100, best_round_points: 4200, best_round_distance_meters: null })).toBe(
            "Best round: 4200 pts",
        );
    });
});

describe("summaryHeadline", () => {
    const players = [
        { profile_id: 1, username: "alice", total_points: 500 },
        { profile_id: 2, username: "bob", total_points: 300 },
    ];

    test("solo sessions always get the neutral message, regardless of score", () => {
        expect(summaryHeadline(players, false, 1)).toEqual({ heading: "Nice work!", icon: "explore" });
    });

    test("the viewer winning gets a first-person callout", () => {
        expect(summaryHeadline(players, true, 1)).toEqual({ heading: "You win! 🎉", icon: "emoji_events" });
    });

    test("someone else winning names them", () => {
        // Viewer is bob (id 2); alice (id 1) has the higher score.
        expect(summaryHeadline(players, true, 2)).toEqual({ heading: "alice wins!", icon: "emoji_events" });
    });

    test("a tie gets its own message instead of naming a false leader", () => {
        const tied = [
            { profile_id: 1, username: "alice", total_points: 400 },
            { profile_id: 2, username: "bob", total_points: 400 },
        ];
        expect(summaryHeadline(tied, true, 1)).toEqual({ heading: "It's a tie!", icon: "handshake" });
    });

    test("an empty participant list doesn't crash and falls back to the tie message", () => {
        expect(summaryHeadline([], true, 1)).toEqual({ heading: "It's a tie!", icon: "handshake" });
    });
});

describe("easeOutQuad", () => {
    test("starts at 0 and ends at 1", () => {
        expect(easeOutQuad(0)).toBe(0);
        expect(easeOutQuad(1)).toBe(1);
    });

    test("decelerates - the second half of progress covers less ground than the first", () => {
        const firstHalf = easeOutQuad(0.5) - easeOutQuad(0);
        const secondHalf = easeOutQuad(1) - easeOutQuad(0.5);
        expect(firstHalf).toBeGreaterThan(secondHalf);
    });

    test("clamps out-of-range progress", () => {
        expect(easeOutQuad(-1)).toBe(0);
        expect(easeOutQuad(2)).toBe(1);
    });
});

describe("countUpValue", () => {
    test("starts at 'from' and ends at 'to'", () => {
        expect(countUpValue(0, 5000, 0)).toBe(0);
        expect(countUpValue(0, 5000, 1)).toBe(5000);
    });

    test("counts upward for an increasing value", () => {
        expect(countUpValue(0, 4820, 0.5)).toBeGreaterThan(0);
        expect(countUpValue(0, 4820, 0.5)).toBeLessThan(4820);
    });

    test("counts downward for a decreasing value (e.g. an animated rating drop)", () => {
        const mid = countUpValue(1500, 1400, 0.5);
        expect(mid).toBeLessThan(1500);
        expect(mid).toBeGreaterThan(1400);
    });

    test("a zero-distance count-up stays put throughout", () => {
        expect(countUpValue(100, 100, 0.5)).toBe(100);
    });
});

describe("interpolateLatLng", () => {
    test("starts at 'from' and ends at 'to'", () => {
        expect(interpolateLatLng([40, -74], [41, -75], 0)).toEqual([40, -74]);
        expect(interpolateLatLng([40, -74], [41, -75], 1)).toEqual([41, -75]);
    });

    test("moves toward 'to' as progress increases", () => {
        const [lat, lng] = interpolateLatLng([0, 0], [10, 20], 0.5);
        expect(lat).toBeGreaterThan(0);
        expect(lat).toBeLessThan(10);
        expect(lng).toBeGreaterThan(0);
        expect(lng).toBeLessThan(20);
    });
});
