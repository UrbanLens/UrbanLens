import { describe, expect, test } from "bun:test";

import { htmxDetail } from "./htmx-events";

describe("htmxDetail", () => {
    test("reads the elements and parameters htmx puts on the event", () => {
        const elt = document.createElement("div");
        const parameters: Record<string, unknown> = { q: "x" };
        const detail = htmxDetail(new CustomEvent("htmx:configRequest", { detail: { elt, target: elt, parameters } }));
        expect(detail.elt).toBe(elt);
        expect(detail.target).toBe(elt);
        if (detail.parameters) detail.parameters.active = "ann";
        expect(parameters.active).toBe("ann");
    });

    test("anything missing or of the wrong type reads as null", () => {
        expect(htmxDetail(new Event("htmx:afterSwap"))).toEqual({ elt: null, target: null, parameters: null, xhr: null });
        expect(htmxDetail(new CustomEvent("x", { detail: { elt: "div", target: 3, parameters: "a=1", xhr: "req" } }))).toEqual({ elt: null, target: null, parameters: null, xhr: null });
    });
});
