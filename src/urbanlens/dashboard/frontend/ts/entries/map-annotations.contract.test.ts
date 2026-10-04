/**
 * This entry builds a live Leaflet map inside one init() closure on import, so, like
 * map-page.contract.test.ts, it is checked as text rather than imported and executed.
 */
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const source = readFileSync(join(import.meta.dir, "map-annotations.ts"), "utf8");

describe("dragging the pin's own marker", () => {
    // The site's fetch net toasts any non-2xx it is not told about, on top of the confirm dialog.
    test("asks its 409 wiki-access question without an error toast, since the drag handler reports every failure itself", () => {
        const savePosition = source.match(/const savePosition = \([\s\S]*?(?=mainMarker\.on\("dragend")/)?.[0] ?? "";
        expect(savePosition, "savePosition no longer sits just before the dragend handler").not.toBe("");
        expect(savePosition).toMatch(/__ulReported: true/);
    });
});

describe("child pin details", () => {
    const urls = ["cfg.markupJsonUrl", "cfg.detailPinsJsonUrl", "cfg.photoGalleryJsonUrl", "boundaryApiUrl"];

    test("every layer that reads the setting asks with it", () => {
        for (const url of urls) {
            const bare = new RegExp(`(?<!childDetailsUrl\\()${url.replace(".", "\\.")}\\b(?!\\s*=)`, "g");
            expect([url, source.match(bare) ?? []]).toEqual([url, []]);
        }
    });

    test("a change reloads each of them", () => {
        const listener = source.match(/addEventListener\(CHILD_DETAILS_EVENT,[\s\S]*?\n {4}\}\);/)?.[0] ?? "";
        expect(listener, "the childDetailsChanged listener is gone").not.toBe("");
        for (const reload of ["toolbar.setMarkupJsonUrl(childDetailsUrl(cfg.markupJsonUrl))", "toolbar.loadMarkup()", "loadDetailPins()", "loadPhotoLayer()", "fetchBoundaries(0)"]) {
            expect(listener).toContain(reload);
        }
    });
});
