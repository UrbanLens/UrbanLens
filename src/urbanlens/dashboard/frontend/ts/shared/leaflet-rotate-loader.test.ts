/**
 * Turning a map: the saved bearing (which way is up) against leaflet-rotate's (how far the content
 * turns), and a map that is still built when leaflet-rotate cannot be had.
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { canTurn, ensureLeafletRotate, rotateOptions, toCompassBearing, toContentTurn } from "./leaflet-rotate-loader";

declare const L: typeof import("leaflet");

const realL = (globalThis as Record<string, unknown>).L;

beforeAll(async () => {
    (globalThis as Record<string, unknown>).L = (await import("leaflet")).default;
});

afterAll(() => {
    (globalThis as Record<string, unknown>).L = realL;
});

describe("bearings", () => {
    test("east up is the content turned 270 degrees clockwise, and back", () => {
        expect(toContentTurn(90)).toBe(270);
        expect(toCompassBearing(270)).toBe(90);
    });

    test("north up is no turn either way", () => {
        expect(toContentTurn(0)).toBe(0);
        expect(toCompassBearing(0)).toBe(0);
        expect(toContentTurn(360)).toBe(0);
    });

    test("any input lands in 0..360, and a non-number is north up", () => {
        expect(toContentTurn(-45)).toBe(45);
        expect(toContentTurn(765)).toBe(315);
        expect(toContentTurn(Number.NaN)).toBe(0);
        expect(toContentTurn(Number.POSITIVE_INFINITY)).toBe(0);
    });

    test("the two conversions undo each other", () => {
        for (const b of [0, 1, 45, 90, 179.5, 270, 315]) expect(toCompassBearing(toContentTurn(b))).toBeCloseTo(b, 9);
    });
});

describe("without leaflet-rotate", () => {
    test("no source means no rotation, and the map is built plain", async () => {
        expect(await ensureLeafletRotate(null)).toBe(false);
        expect(await ensureLeafletRotate({ src: "" })).toBe(false);
        expect(rotateOptions(90)).toEqual({});
    });

    test("a plain Leaflet map does not turn", () => {
        const el = document.createElement("div");
        document.body.appendChild(el);
        const map = L.map(el).setView([0, 0], 2);
        expect(canTurn(map)).toBe(false);
        map.remove();
        el.remove();
    });
});
