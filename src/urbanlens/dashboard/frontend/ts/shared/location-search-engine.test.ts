/**
 * parseCoordinates()/isPlusCode() are the "did the user just paste raw coordinates or a Plus Code into the address bar" detectors that.
 */
import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";
import { isPlusCode, LocationSearchEngine, type LocationSearchOptions, parseCoordinates } from "./location-search-engine";

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
        // 151.2093 is out of latitude range (>90) but valid as a longitude, and -33.8678 is only valid as a latitude.
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

describe("geocoding goes through the server's Nominatim proxy", () => {
    const PROXY = "/map/search/autocomplete/nominatim/";
    const realFetch = globalThis.fetch;
    let requested: string[];

    function respondWith(...bodies: Array<{ status?: number; json: unknown }>): void {
        const queue = [...bodies];
        globalThis.fetch = mock(async (url: string | URL | Request) => {
            requested.push(String(url));
            const next = queue.shift() ?? { json: { results: [] } };
            return new Response(JSON.stringify(next.json), { status: next.status ?? 200, headers: { "Content-Type": "application/json" } });
        }) as unknown as typeof fetch;
    }

    function mount(sources: LocationSearchOptions["sources"] = { osmNominatim: { url: PROXY } }) {
        document.body.innerHTML = '<div id="bar"><input id="q"><div id="sugg" hidden></div></div>';
        const input = document.getElementById("q") as HTMLInputElement;
        const suggestions = document.getElementById("sugg")!;
        const selected: Array<{ lat: number; lng: number; title: string }> = [];
        const toasts: Array<[string, string]> = [];
        const engine = LocationSearchEngine.create({
            input,
            suggestions,
            bar: document.getElementById("bar"),
            sources,
            enableMyLocation: false,
            onSelect: (r) => selected.push(r),
            onToast: (level, message) => toasts.push([level, message]),
        });
        return { input, suggestions, engine, selected, toasts };
    }

    function submit(input: HTMLInputElement, query: string): void {
        input.value = query;
        input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    }

    const settle = async (): Promise<void> => {
        for (let i = 0; i < 10; i++) await new Promise((r) => setTimeout(r, 0));
    };

    function params(url: string): URLSearchParams {
        expect(url.startsWith(`${PROXY}?`)).toBe(true);
        return new URL(url, "https://urbanlens.test").searchParams;
    }

    beforeEach(() => {
        requested = [];
    });

    afterEach(() => {
        globalThis.fetch = realFetch;
        document.body.innerHTML = "";
    });

    test("a submitted address is geocoded by the proxy", async () => {
        respondWith({ json: { results: [{ lat: 40.1, lon: -74.2, name: "Old Mill", display_name: "Old Mill, NY" }] } });
        const { input, selected } = mount();

        submit(input, "10 Main Street");
        await settle();

        expect(requested).toHaveLength(1);
        expect(params(requested[0]!).get("q")).toBe("10 Main Street");
        expect(params(requested[0]!).get("limit")).toBe("1");
        expect(params(requested[0]!).get("cached")).toBeNull();
        expect(selected[0]).toMatchObject({ lat: 40.1, lng: -74.2, title: "Old Mill, NY" });
    });

    test("an unavailable proxy is reported as a failure, not as an address that does not exist", async () => {
        respondWith({ status: 502, json: { results: [], unavailable: "failed" } });
        const { input, selected, toasts } = mount();

        submit(input, "10 Main Street");
        await settle();

        expect(selected).toHaveLength(0);
        expect(toasts.map(([level]) => level)).toEqual(["error"]);
    });

    test("typing fills the OpenStreetMap section from the proxy", async () => {
        respondWith({ json: { results: [{ lat: 40.1, lon: -74.2, name: "Old Mill", display_name: "Old Mill, Springfield, NY" }] } });
        const { engine, suggestions } = mount();

        engine.search("old mill");
        await settle();

        expect(requested).toHaveLength(1);
        expect(params(requested[0]!).get("limit")).toBe("5");
        expect(params(requested[0]!).get("cached")).toBe("1");
        expect(suggestions.textContent).toContain("Places & Addresses");
        expect(suggestions.textContent).toContain("Old Mill, Springfield, NY");
    });

    test("a near query finds the anchor, then searches a box around it", async () => {
        respondWith(
            { json: { results: [] } },
            { json: { results: [{ lat: 40, lon: -74, name: "Mill", display_name: "Mill, NY" }] } },
            { json: { results: [{ lat: 40.2, lon: -74.1, name: "Old", display_name: "Old, NY" }] } },
        );
        const { engine, suggestions, selected } = mount();

        engine.search("old mill");
        await settle();
        const near = [...suggestions.querySelectorAll<HTMLElement>(".addr-suggestion--derived")].find((el) => el.textContent?.includes("near mill"))!;
        near.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
        await settle();

        const [anchor, search] = requested.slice(1).map(params);
        expect(anchor!.get("q")).toBe("mill");
        expect(search!.get("q")).toBe("old");
        expect(search!.get("viewbox")).toBe("-74.5,39.5,-73.5,40.5");
        expect(selected[0]).toMatchObject({ lat: 40.2, lng: -74.1 });
    });

    test("without a proxy nothing is geocoded", async () => {
        respondWith();
        const { input, engine, suggestions, toasts } = mount({});

        engine.search("old mill");
        submit(input, "10 Main Street");
        await settle();

        expect(requested).toHaveLength(0);
        expect(suggestions.textContent).not.toContain("Places & Addresses");
        expect(toasts).toEqual([["warning", "Address not found."]]);
    });
});
