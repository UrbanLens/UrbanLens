/** Splitting a stored region into one polygon per part, so each part loads as its own deletable layer (P120). */
import { describe, expect, test } from "bun:test";

import { polygonParts } from "./region-delete";

const square = (x: number): GeoJSON.Position[][] => [
    [
        [x, 0],
        [x + 1, 0],
        [x + 1, 1],
        [x, 1],
        [x, 0],
    ],
];

describe("polygonParts", () => {
    test("a polygon is its own only part", () => {
        const polygon: GeoJSON.Polygon = { type: "Polygon", coordinates: square(0) };
        expect(polygonParts(polygon)).toEqual([polygon]);
    });

    test("a multipolygon splits into one polygon per component", () => {
        expect(polygonParts({ type: "MultiPolygon", coordinates: [square(0), square(5)] })).toEqual([
            { type: "Polygon", coordinates: square(0) },
            { type: "Polygon", coordinates: square(5) },
        ]);
    });

    test("a feature and a collection are unwrapped, and non-polygons dropped", () => {
        const feature: GeoJSON.Feature = { type: "Feature", properties: {}, geometry: { type: "MultiPolygon", coordinates: [square(0), square(5)] } };
        const collection: GeoJSON.GeometryCollection = {
            type: "GeometryCollection",
            geometries: [{ type: "LineString", coordinates: [[0, 0], [1, 1]] }, { type: "Polygon", coordinates: square(9) }],
        };
        expect(polygonParts(feature)).toHaveLength(2);
        expect(polygonParts(collection)).toEqual([{ type: "Polygon", coordinates: square(9) }]);
    });

    test("nothing stored gives no parts", () => {
        expect(polygonParts(null)).toEqual([]);
    });
});
