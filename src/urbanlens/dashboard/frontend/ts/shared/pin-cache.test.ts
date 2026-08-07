/**
 * readCachedPinLocations parses the exact cache shape written by
 * pages/map/index.html's inline script (_writeCache, v8 payload) - these
 * tests write that shape directly rather than importing the map page's script.
 */
import { beforeEach, describe, expect, test } from "bun:test";
import { readCachedPinLocations } from "./pin-cache";

// localStorage comes from the DOM the test preload registers (see testing/dom-setup.ts),
// so these exercise a real Storage rather than a hand-rolled stand-in.

const PROFILE_UUID = "11111111-1111-1111-1111-111111111111";

function writeCache(overrides: Record<string, unknown> = {}): void {
    localStorage.setItem(
        `ul_pins_v5_${PROFILE_UUID}`,
        JSON.stringify({
            v: 8,
            ts: Date.now(),
            profileUuid: PROFILE_UUID,
            appUuid: "app-1",
            pins: {
                "pin-a": { uuid: "pin-a", latitude: 40.1, longitude: -75.1 },
                "pin-b": { uuid: "pin-b", latitude: 41.2, longitude: -76.2 },
            },
            ...overrides,
        }),
    );
}

beforeEach(() => {
    localStorage.clear();
});

describe("readCachedPinLocations", () => {
    test("returns an empty array with no profile uuid", () => {
        expect(readCachedPinLocations("")).toEqual([]);
    });

    test("returns an empty array when nothing is cached", () => {
        expect(readCachedPinLocations(PROFILE_UUID)).toEqual([]);
    });

    test("parses lat/lng out of a valid v8 cache", () => {
        writeCache();
        expect(readCachedPinLocations(PROFILE_UUID)).toEqual([
            { latitude: 40.1, longitude: -75.1 },
            { latitude: 41.2, longitude: -76.2 },
        ]);
    });

    test("ignores a cache for a different profile", () => {
        writeCache({ profileUuid: "other-profile" });
        expect(readCachedPinLocations(PROFILE_UUID)).toEqual([]);
    });

    test("ignores a stale cache version", () => {
        writeCache({ v: 7 });
        expect(readCachedPinLocations(PROFILE_UUID)).toEqual([]);
    });

    test("skips pins with missing/invalid coordinates", () => {
        localStorage.setItem(
            `ul_pins_v5_${PROFILE_UUID}`,
            JSON.stringify({
                v: 8,
                profileUuid: PROFILE_UUID,
                pins: {
                    good: { latitude: 40.1, longitude: -75.1 },
                    bad: { latitude: null, longitude: undefined },
                },
            }),
        );
        expect(readCachedPinLocations(PROFILE_UUID)).toEqual([{ latitude: 40.1, longitude: -75.1 }]);
    });

    test("returns an empty array for malformed JSON", () => {
        localStorage.setItem(`ul_pins_v5_${PROFILE_UUID}`, "{not json");
        expect(readCachedPinLocations(PROFILE_UUID)).toEqual([]);
    });
});
