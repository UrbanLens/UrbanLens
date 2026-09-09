/**
 * Guards the contract between `IconPicker.search` and the partials it reads.
 *
 * `search()` lowercases the query and then compares it against `data-label`
 * exactly - `label.includes(q)`. That is only case-insensitive because the
 * markup writes `data-label="{{ label|lower }}"`. Drop the `|lower` and
 * searching "cam" stops matching an icon labelled "Camera": no error, no
 * empty-state, just a grid that quietly hides everything the user typed a
 * capital letter into.
 *
 * The catalogue moved out of `_icon_picker.html` into
 * `_icon_picker_grid_items.html`, which is fetched once and shared by every
 * picker (P68). Both are read here: checking only the first would leave this
 * test passing against the one remaining literal `data-label` on the "None"
 * button while the 1,249 that matter went unwatched.
 *
 * This is the same shape as `pin-cache.contract.test.ts` - two sides of one
 * agreement, written in different languages, held together by nothing but
 * convention - and that one already drifted once in this codebase.
 *
 * The Python half is covered elsewhere: `test_icon_metadata` asserts every
 * `ICON_KEYWORDS` value is lowercase. It is the template's `|lower` that had
 * nothing watching it.
 */

import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { IconPicker } from "./icon-picker";

const TEMPLATE_DIR = join(import.meta.dir, "../../../templates/dashboard/partials/ui");
const template = readFileSync(join(TEMPLATE_DIR, "_icon_picker.html"), "utf8");
const gridTemplate = readFileSync(join(TEMPLATE_DIR, "_icon_picker_grid_items.html"), "utf8");
const mapPage = readFileSync(join(import.meta.dir, "../../../templates/dashboard/pages/map/index.html"), "utf8");

function buildPicker(label: string): HTMLElement {
    document.body.innerHTML = `
        <div id="icon-panel-x" class="icon-picker-panel">
            <button class="icon-tab" data-cat=""></button>
            <div id="icon-grid-x">
                <button class="icon-picker-item" data-icon="photo_camera" data-label="${label}" data-keywords=""></button>
            </div>
        </div>`;
    return document.querySelector<HTMLElement>(".icon-picker-item")!;
}

describe("icon picker search contract with _icon_picker.html", () => {
    test("the partials are where we think they are", () => {
        expect(template).toContain("icon-picker-item");
        expect(gridTemplate).toContain("icon-picker-item");
    });

    test("the catalogue lives in the shared grid partial, not in the per-picker one", () => {
        // The inner loop, over `cat_data.1`, is the 1,249 icons. The per-picker
        // partial still loops the categories themselves for its tab strip, which
        // is a handful of buttons and was never the cost.
        expect(template).not.toContain("cat_data.1");
        expect(gridTemplate).toContain("cat_data.1");
    });

    test("data-label is emitted lowercased", () => {
        // `?? ""` rather than a non-null assertion: the group always matches, but
        // an empty attribute is a legitimate value the assertion below handles.
        const labelAttributes = [template, gridTemplate].flatMap((source) =>
            [...source.matchAll(/data-label="([^"]*)"/g)].map((m) => m[1] ?? ""),
        );

        expect(labelAttributes.length).toBeGreaterThan(0);
        for (const value of labelAttributes) {
            // Either a literal (already lowercase) or a variable piped through `lower`.
            const isTemplateExpression = value.includes("{{");
            const claim = isTemplateExpression ? /\|\s*lower/.test(value) : value === value.toLowerCase();
            expect(claim).toBe(true);
        }
    });

    test("a grid button resolves its picker from the enclosing dropdown", () => {
        // One cached response serves every picker, so the id cannot be baked in.
        expect(gridTemplate).toContain("closest('.icon-picker-dropdown').dataset.picker");
        expect(gridTemplate).not.toContain("picker_id");
    });

    test("search matches a lowercased label regardless of query case", () => {
        const item = buildPicker("camera");

        IconPicker.search("x", "CAM");

        expect(item.style.display).toBe("");
    });

    test("search would miss a label the template had not lowercased", () => {
        // Demonstrates why the contract above matters rather than asserting it
        // twice: this is exactly what dropping `|lower` would produce.
        const item = buildPicker("Camera");

        IconPicker.search("x", "cam");

        expect(item.style.display).toBe("none");
    });

    test("an empty query shows everything again", () => {
        const item = buildPicker("camera");
        IconPicker.search("x", "zzz");
        expect(item.style.display).toBe("none");

        IconPicker.search("x", "   ");

        expect(item.style.display).toBe("");
    });
});

/**
 * The map page defines its own IconPicker - its `pick()` and `_handleUpload()`
 * carry add-pin behaviour the shared module has no business knowing - so it
 * cannot import this one, and the lazy-fetch logic exists twice.
 *
 * Nothing else watches that copy. It is inline JS inside an HTML template, so
 * `tsc` cannot see it and no unit test can import it, and the first hand-written
 * version dropped `reapplyFilter` - a search typed while the catalogue was
 * loading was silently discarded the moment it arrived. The shared module has a
 * test for exactly that case; the copy had nothing.
 *
 * These assert the four behaviours that make the fetch safe, by name. A
 * structural check is weaker than running the code, and is what is available:
 * the alternative was no check at all, which is what let the two diverge.
 */
describe("the map page's inline copy of the lazy grid fetch", () => {
    test("it re-applies the current filter rather than resetting it", () => {
        // The divergence that actually happened: `setTabSilent(id, '')` in the
        // fetch's completion handler throws away a search typed while it ran.
        expect(mapPage).toContain("_reapplyIconFilter");
        expect(mapPage).toContain("_fillIconGrid(id).then(function() { _reapplyIconFilter(id); });");
    });

    test("a failed fetch clears the cached request so the next open retries", () => {
        expect(mapPage).toContain("_iconGridRequest = null;");
    });

    test("a second open does not append a second copy of the catalogue", () => {
        expect(mapPage).toContain("if (grid.dataset.iconsLoaded === '1') return;");
    });

    test("it marks the current value selected once the grid arrives", () => {
        // The server used to render `selected` into the matching button; one
        // shared response cannot, so both copies have to reapply it.
        expect(mapPage).toContain("item.classList.toggle('selected', item.dataset.icon === current);");
    });

    test("it reads both fragments of the shared response", () => {
        expect(mapPage).toContain("data-icon-picker-tabs");
        expect(mapPage).toContain("data-icon-picker-items");
    });
});
