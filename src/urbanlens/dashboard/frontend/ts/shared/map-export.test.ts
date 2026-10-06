/**
 * What a download covers when the map has been turned: enough tiles under every corner of the turned
 * view, placed relative to its centre, and the credit written into the image in full.
 */
import { describe, expect, test } from "bun:test";
import { frameBounds, frameTiles, wrapCredit, type ExportFrame } from "./map-export";

const FRAME: ExportFrame = { width: 600, height: 400, zoom: 15, center: { x: 1_000_000, y: 1_000_000 }, turn: 0 };

describe("frameBounds", () => {
    test("an unturned view covers exactly itself", () => {
        expect(frameBounds(FRAME)).toEqual({ minX: 999_700, minY: 999_800, maxX: 1_000_300, maxY: 1_000_200 });
    });

    test("a quarter turn swaps width and height", () => {
        const b = frameBounds({ ...FRAME, turn: Math.PI / 2 });
        expect(b.maxX - b.minX).toBeCloseTo(400, 6);
        expect(b.maxY - b.minY).toBeCloseTo(600, 6);
    });

    test("an eighth turn covers the box around the turned rectangle, wider and taller than the view", () => {
        const b = frameBounds({ ...FRAME, turn: Math.PI / 4 });
        const side = (600 + 400) * Math.SQRT1_2;
        expect(b.maxX - b.minX).toBeCloseTo(side, 6);
        expect(b.maxY - b.minY).toBeCloseTo(side, 6);
        expect((b.minX + b.maxX) / 2).toBeCloseTo(FRAME.center.x, 6);
    });

    test("turning either way covers the same area", () => {
        expect(frameBounds({ ...FRAME, turn: -0.3 })).toEqual(frameBounds({ ...FRAME, turn: 0.3 }));
    });
});

describe("frameTiles", () => {
    function covers(frame: ExportFrame, fetchZoom: number): boolean {
        const b = frameBounds(frame);
        const tiles = frameTiles(frame, fetchZoom);
        const minLeft = Math.min(...tiles.map((t) => t.left)) + frame.center.x;
        const minTop = Math.min(...tiles.map((t) => t.top)) + frame.center.y;
        const maxRight = Math.max(...tiles.map((t) => t.left + t.size)) + frame.center.x;
        const maxBottom = Math.max(...tiles.map((t) => t.top + t.size)) + frame.center.y;
        return minLeft <= b.minX && minTop <= b.minY && maxRight >= b.maxX && maxBottom >= b.maxY;
    }

    test("the tiles cover the whole view, turned or not", () => {
        for (const turn of [0, 0.4, Math.PI / 4, Math.PI / 2, 2.5]) expect(covers({ ...FRAME, turn }, 15)).toBe(true);
    });

    test("a turned view needs more tiles than an unturned one", () => {
        expect(frameTiles({ ...FRAME, turn: Math.PI / 4 }, 15).length).toBeGreaterThan(frameTiles(FRAME, 15).length);
    });

    test("tiles from a coarser zoom are drawn larger, and still cover the view", () => {
        const tiles = frameTiles(FRAME, 13);
        expect(tiles.every((t) => t.size === 1024 && t.z === 13)).toBe(true);
        expect(covers(FRAME, 13)).toBe(true);
    });

    test("each tile is placed relative to the view's centre", () => {
        const tile = frameTiles(FRAME, 15).find((t) => t.x === Math.floor(1_000_000 / 256) && t.y === Math.floor(1_000_000 / 256))!;
        expect(tile.left).toBe(Math.floor(1_000_000 / 256) * 256 - 1_000_000);
        expect(tile.top).toBe(tile.left);
    });

    test("there is no tile off the top or bottom of the world, and sideways it repeats", () => {
        const edge: ExportFrame = { width: 600, height: 400, zoom: 2, center: { x: 0, y: 0 }, turn: 0 };
        const tiles = frameTiles(edge, 2);
        expect(tiles.every((t) => t.y >= 0 && t.y < 4)).toBe(true);
        expect(tiles.every((t) => t.x >= 0 && t.x < 4)).toBe(true);
        // Left of x=0 wraps round to the far side.
        expect(tiles.some((t) => t.x === 3 && t.left < 0)).toBe(true);
    });
});

describe("wrapCredit", () => {
    const measure = (line: string): number => line.length * 6;

    test("a credit that fits stays on one line", () => {
        expect(wrapCredit("Powered by Esri · Vantor", 600, measure)).toEqual(["Powered by Esri · Vantor"]);
    });

    test("a long credit breaks between words, and loses none of them", () => {
        const credit = "Powered by Esri · Esri Canada, Esri, HERE, Garmin, INCREMENT P, USGS, METI/NASA, EPA, USDA";
        const lines = wrapCredit(credit, 180, measure);
        expect(lines.length).toBeGreaterThan(1);
        expect(lines.every((line) => measure(line) <= 180 || !line.includes(" "))).toBe(true);
        expect(lines.join(" ")).toBe(credit);
    });

    test("a word wider than the line keeps a line of its own rather than being cut", () => {
        expect(wrapCredit("a supercalifragilistic b", 30, measure)).toEqual(["a", "supercalifragilistic", "b"]);
    });

    test("an empty credit is no lines", () => {
        expect(wrapCredit("", 100, measure)).toEqual([]);
    });
});
