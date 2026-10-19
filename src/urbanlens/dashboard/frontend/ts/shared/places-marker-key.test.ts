import { describe, expect, test } from "bun:test";
import { forgetPlacesMarker, placesMarkerKey } from "./places-marker-key";

describe("placesMarkerKey", () => {
    test("prefers the Google id", () => {
        expect(placesMarkerKey({ place_id: "p1", place_source: "osm", source_id: "way/9" })).toBe("p1");
    });
    test("falls back to the dataset record when the id is blank", () => {
        expect(placesMarkerKey({ place_id: "", place_source: "osm", source_id: "way/9", source: "google" })).toBe("osm:way/9");
        expect(placesMarkerKey({ source: "google", source_id: "7" })).toBe("google:7");
    });
    test("is empty for a place with neither", () => {
        expect(placesMarkerKey({ place_id: "" })).toBe("");
    });
});

describe("forgetPlacesMarker", () => {
    test("removes a marker of a place with no Google id after it is pinned", () => {
        const key = placesMarkerKey({ place_id: "", place_source: "osm", source_id: "way/9" });
        const markers = new Map([[key, "marker"]]);
        const removed: string[] = [];
        forgetPlacesMarker(markers, { removeLayer: (m) => removed.push(m) }, key);
        expect(removed).toEqual(["marker"]);
        expect(markers.size).toBe(0);
    });
    test("ignores an unknown or empty key", () => {
        const markers = new Map([["a", "m"]]);
        const removed: string[] = [];
        forgetPlacesMarker(markers, { removeLayer: (m) => removed.push(m) }, "b");
        forgetPlacesMarker(markers, { removeLayer: (m) => removed.push(m) }, null);
        expect(removed).toEqual([]);
        expect(markers.size).toBe(1);
    });
});
