import { describe, expect, test } from "bun:test";

import { destinationSearchHistoryKey, readonlyMarkupShapes } from "./safety-map";

const LAT = 40.7128;
const LNG = -74.006;
const D = 0.004;

function item(markup_type: string, geometry: Record<string, unknown>): Record<string, unknown> {
    return { uuid: markup_type, markup_type, geometry, color: "#e53e3e", stroke_width: 3 };
}

describe("readonlyMarkupShapes", () => {
    // The contact portal drew a polygon with its latitude and longitude swapped, and a square not at all.
    test("areas land where they were drawn", () => {
        const shapes = readonlyMarkupShapes({
            markup_items: [
                item("polygon", { type: "Polygon", coordinates: [[[LNG - D, LAT], [LNG, LAT + D], [LNG + D, LAT], [LNG - D, LAT]]] }),
                item("square", { type: "Polygon", coordinates: [[[LNG - D, LAT - D], [LNG, LAT - D], [LNG, LAT], [LNG - D, LAT], [LNG - D, LAT - D]]] }),
            ],
        });
        expect(shapes).toEqual([
            expect.objectContaining({ type: "polygon", latlngs: [[LAT, LNG - D], [LAT + D, LNG], [LAT, LNG + D]] }),
            expect.objectContaining({ type: "rect", latlngs: [[LAT - D, LNG - D], [LAT, LNG]] }),
        ]);
    });

    test("lines, circles and text", () => {
        const shapes = readonlyMarkupShapes({
            markup_items: [
                item("line", { type: "LineString", coordinates: [[LNG, LAT], [LNG + D, LAT + D]] }),
                item("circle", { type: "Circle", coordinates: [LNG, LAT], radius: 150 }),
                { ...item("text", { type: "Point", coordinates: [LNG, LAT] }), label: "Gate" },
            ],
        });
        expect(shapes.map((s) => s.type)).toEqual(["line", "circle", "text"]);
        expect(shapes[0]?.latlngs).toEqual([[LAT, LNG], [LAT + D, LNG + D]]);
        expect(shapes[1]?.latlngs[0]).toEqual([LAT, LNG]);
        expect(shapes[2]?.label).toBe("Gate");
    });

    test("a malformed or missing payload renders nothing", () => {
        expect(readonlyMarkupShapes(null)).toEqual([]);
        expect(readonlyMarkupShapes({ markup_items: [item("polygon", {})] })).toEqual([]);
    });
});

describe("destinationSearchHistoryKey", () => {
    test("is the viewer's own, and the shared key it replaced is dropped", () => {
        localStorage.setItem("ul_safety_dest_history_v1", JSON.stringify(["Old Asylum"]));
        expect(destinationSearchHistoryKey("0f3c")).toBe("ul_safety_dest_history_v1_0f3c");
        expect(localStorage.getItem("ul_safety_dest_history_v1")).toBeNull();
    });
});
