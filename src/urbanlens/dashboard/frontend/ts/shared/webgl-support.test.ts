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
        // happy-dom (this test harness's DOM) has no WebGL2 backend, so this cannot assert a
        // specific true/false outcome without coupling the test to that DOM stub's incidental
        // behavior - it only proves document.createElement("canvas").getContext("webgl2") runs to
        // completion and answers a boolean, not that it detects a real browser's support correctly.
        expect(typeof supportsWebGL2()).toBe("boolean");
    });

    test("caches the default probe - a second no-argument call creates no further canvas", () => {
        let canvasesCreated = 0;
        const original = document.createElement.bind(document);
        document.createElement = ((tagName: string, options?: unknown) => {
            if (tagName === "canvas") canvasesCreated++;
            return original(tagName, options as never);
        }) as typeof document.createElement;
        try {
            supportsWebGL2();
            const createdAfterFirstCall = canvasesCreated;
            supportsWebGL2();
            expect(canvasesCreated).toBe(createdAfterFirstCall);
        } finally {
            document.createElement = original;
        }
    });

    test("never caches an injected factory - each call gets its own fresh probe", () => {
        let invocations = 0;
        const createCanvas = (): WebGL2ProbeCanvas => {
            invocations++;
            return { getContext: () => ({ __fake: "gl2-context" }) };
        };
        supportsWebGL2(createCanvas);
        supportsWebGL2(createCanvas);
        expect(invocations).toBe(2);
    });
});
