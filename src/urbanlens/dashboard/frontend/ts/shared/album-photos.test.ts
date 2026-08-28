import { describe, expect, test } from "bun:test";

import { albumMatchesQuery } from "./album-picker";
import { parsePhotoIds } from "./photo-tile";
import { bufferedRange, isFarFromViewport, shouldFetchNextPage } from "./photo-virtual-grid";

describe("albumMatchesQuery", () => {
    test("an empty query matches every album", () => {
        expect(albumMatchesQuery("Interior 2019", "")).toBe(true);
        expect(albumMatchesQuery("Interior 2019", "   ")).toBe(true);
    });

    test("filters by case-insensitive substring", () => {
        expect(albumMatchesQuery("Interior 2019", "inter")).toBe(true);
        expect(albumMatchesQuery("Interior 2019", "EXTERIOR")).toBe(false);
    });
});

describe("photo id drag payload", () => {
    test("parses a JSON list of ids", () => {
        const data = { getData: () => "[4,8,15]" } as unknown as DataTransfer;
        expect(parsePhotoIds(data)).toEqual([4, 8, 15]);
    });

    test("drops non-numeric junk", () => {
        const data = { getData: () => "[1, \"x\", 2]" } as unknown as DataTransfer;
        expect(parsePhotoIds(data)).toEqual([1, 2]);
    });
});

describe("photo virtual grid windowing", () => {
    test("buffers tiles on both sides of the visible range", () => {
        expect(bufferedRange(10, 14, 100, 4)).toEqual({ start: 6, end: 19 });
    });

    test("clamps to the ends of the list", () => {
        expect(bufferedRange(0, 2, 10, 4)).toEqual({ start: 0, end: 7 });
        expect(bufferedRange(8, 9, 10, 4)).toEqual({ start: 4, end: 10 });
    });

    test("unloads tiles well above or below the viewport", () => {
        expect(isFarFromViewport(-2000, -1800, 800, 1200)).toBe(true);
        expect(isFarFromViewport(50, 160, 800, 1200)).toBe(false);
    });

    test("fetches the next page only when the sentinel is on screen and more remain", () => {
        expect(shouldFetchNextPage(48, 200, true)).toBe(true);
        expect(shouldFetchNextPage(200, 200, true)).toBe(false);
        expect(shouldFetchNextPage(48, 200, false)).toBe(false);
    });
});
