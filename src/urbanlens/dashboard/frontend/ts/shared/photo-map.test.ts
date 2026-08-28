/**
 * Photo marker sizing and the stacked-cluster badge. Leaflet is not loaded
 * here - these are the pure bits a second map surface (album, pin detail)
 * must stay in lockstep with.
 */
import { describe, expect, test } from "bun:test";

import { detailPinClusterRadius } from "./map-clusters";
import { PHOTO_MARKER_BASE_SIZE, PHOTO_MARKER_MIN_SIZE, photoClusterMarkup, photoClusterRadius, photoMarkerSize } from "./photo-map";

describe("photoMarkerSize", () => {
    test("is full size at zoom 16 and above", () => {
        expect(photoMarkerSize(16)).toBe(PHOTO_MARKER_BASE_SIZE);
        expect(photoMarkerSize(20)).toBe(PHOTO_MARKER_BASE_SIZE);
    });

    test("never shrinks below the readable floor", () => {
        expect(photoMarkerSize(0)).toBe(PHOTO_MARKER_MIN_SIZE);
        expect(photoMarkerSize(8)).toBe(PHOTO_MARKER_MIN_SIZE);
    });
});

describe("photoClusterRadius", () => {
    test("tracks thumbnail size so overlapping squares become a stack", () => {
        expect(photoClusterRadius(16)).toBe(Math.round(PHOTO_MARKER_BASE_SIZE * 0.9));
        expect(photoClusterRadius(8)).toBe(PHOTO_MARKER_MIN_SIZE);
    });

    test("stays wide at building-level zoom so same-spot GPS photos remain stacked", () => {
        expect(photoClusterRadius(18)).toBeGreaterThan(detailPinClusterRadius(18));
        expect(photoClusterRadius(18)).toBeGreaterThan(20);
    });
});

describe("photoClusterMarkup", () => {
    test("stacks the top image over the second and shows the count", () => {
        const html = photoClusterMarkup("https://example.test/a.jpg", "https://example.test/b.jpg", 5, 44);
        expect(html).toContain("photo-cluster__img--front");
        expect(html).toContain("photo-cluster__img--back");
        expect(html).toContain('src="https://example.test/a.jpg"');
        expect(html).toContain('src="https://example.test/b.jpg"');
        expect(html).toContain('aria-label="5 photos"');
        expect(html).toContain(">5<");
        expect(html).not.toContain("marker-cluster");
    });

    test("escapes quotes in photo URLs so they cannot break the attribute", () => {
        const html = photoClusterMarkup('https://example.test/a".jpg', "https://example.test/b.jpg", 2, 44);
        expect(html).toContain("a&quot;.jpg");
        expect(html).not.toContain('src="https://example.test/a"');
    });

    test("falls back to the front image when no second photo is given", () => {
        const html = photoClusterMarkup("https://example.test/a.jpg", "", 2, 44);
        expect((html.match(/https:\/\/example\.test\/a\.jpg/g) || []).length).toBe(2);
    });
});
