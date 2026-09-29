import { describe, expect, test } from "bun:test";

import { byId, formControlById } from "./dom";

describe("byId", () => {
    test("returns the element only when it is the asked-for type", () => {
        document.body.innerHTML = '<input id="a"><div id="b"></div>';
        expect(byId("a", HTMLInputElement)?.id).toBe("a");
        expect(byId("b", HTMLInputElement)).toBeNull();
        expect(byId("b", HTMLElement)?.id).toBe("b");
        expect(byId("missing", HTMLElement)).toBeNull();
    });

    test("formControlById accepts any value-bearing control and nothing else", () => {
        document.body.innerHTML = '<input id="i"><textarea id="t"></textarea><select id="s"></select><div id="d"></div>';
        expect(["i", "t", "s", "d"].map((id) => formControlById(id)?.id ?? null)).toEqual(["i", "t", "s", null]);
    });
});
