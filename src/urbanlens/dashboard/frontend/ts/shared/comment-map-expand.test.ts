import { beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { installGlobalCommentMapExpand } from "./comment-map-expand";

let expanded: string[] = [];

beforeAll(() => {
    installGlobalCommentMapExpand();
    installGlobalCommentMapExpand();
});

beforeEach(() => {
    expanded = [];
    window._expandCommentMap = (id) => void expanded.push(id);
    document.body.innerHTML = `
      <div class="comment-map-preview" data-comment-id="visit-7">
        <button type="button" class="visit-map-open" data-comment-map-expand="visit-7"><span><i>open_in_full</i></span></button>
      </div>
      <button type="button" data-comment-map-expand="">empty</button>`;
});

describe("data-comment-map-expand", () => {
    test("opens the named map once, from anywhere inside the control", () => {
        document.querySelector<HTMLElement>(".visit-map-open i")?.click();
        expect(expanded).toEqual(["visit-7"]);
    });

    test("a control naming no map does nothing", () => {
        document.querySelector<HTMLElement>('[data-comment-map-expand=""]')?.click();
        expect(expanded).toEqual([]);
    });

    test("a page without the map viewer ignores the click", () => {
        window._expandCommentMap = undefined;
        expect(() => document.querySelector<HTMLElement>(".visit-map-open")?.click()).not.toThrow();
    });
});
