import { describe, expect, test } from "bun:test";
import { supportsWebGL2, type WebGL2ProbeCanvas } from "./webgl-support";

function fakeCanvas(context: unknown): () => WebGL2ProbeCanvas {
    return () => ({
        getContext: (contextId) => {
            expect(contextId).toBe("webgl2");
            return context;
        },
    });
}

describe("supportsWebGL2", () => {
    test("true when the browser returns a context", () => {
        expect(supportsWebGL2(fakeCanvas({ __fake: "gl2-context" }))).toBe(true);
    });

    test("false when the browser returns null (real WebGL2 absence)", () => {
        expect(supportsWebGL2(fakeCanvas(null))).toBe(false);
    });

    test("false when the browser returns undefined", () => {
        expect(supportsWebGL2(fakeCanvas(undefined))).toBe(false);
    });

    test("false, not a thrown error, when getContext itself throws", () => {
        const createCanvas = (): WebGL2ProbeCanvas => ({
            getContext: () => {
                throw new Error("WebGL2 disabled by enterprise policy");
            },
        });
        expect(supportsWebGL2(createCanvas)).toBe(false);
    });

    test("defaults to probing a real <canvas> when no factory is given", () => {
        // happy-dom (this test harness's DOM) has no WebGL2 backend, so the real default path
        // always resolves to false here - this only proves the default path runs without throwing,
        // not that it detects a real browser's support correctly.
        expect(supportsWebGL2()).toBe(false);
    });
});
