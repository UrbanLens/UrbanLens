import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { readMapFilterResults } from "./map-filter-results";

const TEMPLATES = join(import.meta.dir, "../../../templates/dashboard/pages/map");

function body(html: string): HTMLElement {
    const root = document.createElement("div");
    root.id = "map-body";
    root.innerHTML = html;
    return root;
}

function doc(id: string, value: unknown): string {
    return `<script id="${id}" type="application/json">${JSON.stringify(value)}</script>`;
}

describe("readMapFilterResults", () => {
    test("reads a payload result set with its labels and meta", () => {
        const root = body(
            doc("map-filter-pins", [{ uuid: "a", latitude: 1, longitude: 2 }]) +
                doc("map-filter-labels", { "7": { name: "x" } }) +
                doc("map-filter-meta", { truncated: true, total: 40 }),
        );
        const results = readMapFilterResults<{ uuid: string; latitude: number; longitude: number }, Record<string, unknown>>(root);
        expect(results).toEqual({
            kind: "payloads",
            pins: [{ uuid: "a", latitude: 1, longitude: 2 }],
            labels: { "7": { name: "x" } },
            meta: { truncated: true, total: 40 },
        });
    });

    test("reads an identifier result set", () => {
        const root = body(doc("map-filter-uuids", ["a", "b"]) + doc("map-filter-meta", { truncated: false, total: 2 }));
        expect(readMapFilterResults(root)).toEqual({ kind: "identifiers", uuids: ["a", "b"], meta: { truncated: false, total: 2 } });
    });

    test("an empty payload set is still a result set, so the map clears", () => {
        const root = body(doc("map-filter-pins", []) + doc("map-filter-meta", { truncated: false, total: 0 }));
        expect(readMapFilterResults(root)?.kind).toBe("payloads");
    });

    test("a swap without result documents is not a result set", () => {
        expect(readMapFilterResults(body("<p>nothing</p>"))).toBeNull();
    });
});

// The map page's script is an ES module, so its bindings are not globals: an
// inline script in these partials that names one throws a ReferenceError.
describe("the filter result partials", () => {
    for (const name of ["data.html", "data_ids.html"]) {
        test(`${name} carries data only, no inline script`, () => {
            const source = readFileSync(join(TEMPLATES, name), "utf8");
            expect(source).not.toMatch(/<script/i);
            expect(source).toContain('id="map-body"');
        });
    }
});
