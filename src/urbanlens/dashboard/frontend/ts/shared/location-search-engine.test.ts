/**
 * parseCoordinates()/isPlusCode() are the "did the user just paste raw
 * coordinates or a Plus Code into the address bar" detectors that run before
 * any network geocoding call, on the main map and safety check-in address
 * bars alike. The regex/heuristic logic is easy to get subtly wrong (axis
 * order, DMS parsing, Plus Code shorthand) and has no DOM dependency, so it's
 * tested directly rather than through the DOM-bound engine.
 */
import { describe, expect, test } from "bun:test";
import { isPlusCode, parseCoordinates } from "./location-search-engine";

describe("parseCoordinates", () => {
    test("parses comma-separated decimal lat,lng", () => {
        expect(parseCoordinates("40.7128,-74.0060")).toEqual({ lat: 40.7128, lng: -74.006 });
    });

    test("parses space-separated decimal lat lng", () => {
        expect(parseCoordinates("40.7128 -74.0060")).toEqual({ lat: 40.7128, lng: -74.006 });
    });

    test("tolerates a comma plus extra whitespace", () => {
        expect(parseCoordinates("  40.7128 ,  -74.0060  ")).toEqual({ lat: 40.7128, lng: -74.006 });
    });

    test("swaps axis order when the first number can only be a longitude", () => {
        // 151.2093 is out of latitude range (>90) but valid as a longitude,
        // and -33.8678 is only valid as a latitude - the engine should
        // recover the intended point (Sydney) rather than reject it.
        expect(parseCoordinates("151.2093,-33.8678")).toEqual({ lat: -33.8678, lng: 151.2093 });
    });

    test("does not swap when both orderings would be structurally valid", () => {
        // Both values fall within [-90, 90], so the first-wins rule applies
        // literally: the string is read as lat,lng in the order given.
        expect(parseCoordinates("-74.0060,40.7128")).toEqual({ lat: -74.006, lng: 40.7128 });
    });

    test("rejects points where neither axis order is in range", () => {
        expect(parseCoordinates("200,300")).toBeNull();
    });

    test("parses DMS coordinates", () => {
        const result = parseCoordinates(`40°42'46"N 74°0'21"W`);
        expect(result).not.toBeNull();
        expect(result!.lat).toBeCloseTo(40.7128, 3);
        expect(result!.lng).toBeCloseTo(-74.0058, 3);
    });

    test("DMS south/west hemispheres negate correctly", () => {
        const result = parseCoordinates(`33°52'4"S 151°12'36"E`);
        expect(result).not.toBeNull();
        expect(result!.lat).toBeCloseTo(-33.8678, 3);
        expect(result!.lng).toBeCloseTo(151.21, 2);
    });

    test("returns null for plain search text", () => {
        expect(parseCoordinates("abandoned mall near me")).toBeNull();
        expect(parseCoordinates("")).toBeNull();
    });
});

describe("isPlusCode", () => {
    test("accepts a full-length Plus Code", () => {
        expect(isPlusCode("87G8Q23F+GJ")).toBe(true);
    });

    test("accepts a shortened Plus Code with a locality suffix", () => {
        expect(isPlusCode("CWC8+R9 Mountain View")).toBe(true);
    });

    test("is case-insensitive", () => {
        expect(isPlusCode("cwc8+r9")).toBe(true);
    });

    test("tolerates surrounding whitespace", () => {
        expect(isPlusCode("  87G8Q23F+GJ  ")).toBe(true);
    });

    test("rejects plain addresses and search text", () => {
        expect(isPlusCode("1600 Amphitheatre Parkway")).toBe(false);
        expect(isPlusCode("abandoned mall")).toBe(false);
        expect(isPlusCode("")).toBe(false);
    });

    test("rejects strings without a + separator", () => {
        expect(isPlusCode("87G8Q23FGJ")).toBe(false);
    });
});
