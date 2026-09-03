/**
 * Guards the one thing that keeps a loading placeholder out of the lightbox.
 *
 * The Vault grids give a skeleton tile the *same* base class as a real one
 * (`photo-tile photo-tile--skeleton`), differing only by the modifier and by
 * carrying no `data-id`. The page's lightbox builder collects tiles from the
 * DOM, so the `[data-id]` in its selector is the only thing standing between an
 * in-flight page fetch and a lightbox entry with `imageId: NaN` and no url -
 * reachable by prev/next, and blank.
 *
 * The selector lives in an inline `<script>` in the page template, which
 * `bun run typecheck` and the rest of the TS suite cannot see, and the skeleton
 * class lives here. Nothing but this test holds the two together.
 */

import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { renderVaultSkeletonTile } from "./vault-photo-grid";
import { renderVaultDocumentSkeletonTile } from "./vault-document-grid";

const PAGES = join(import.meta.dir, "../../../templates/dashboard/pages/vault");

const KINDS = [
    { name: "photos", template: "photos.html", opener: "photosOpenLightbox", tile: "photo-tile", render: renderVaultSkeletonTile },
    { name: "documents", template: "documents.html", opener: "documentsOpenLightbox", tile: "document-tile", render: renderVaultDocumentSkeletonTile },
];

describe.each(KINDS)("$name lightbox tile collection", ({ template, opener, tile, render }) => {
    const source = readFileSync(join(PAGES, template), "utf8");

    test("the template still defines the lightbox opener", () => {
        // Without this, the selector assertions below pass by matching nothing.
        expect(source).toContain(opener);
    });

    test("every tile query filters on data-id", () => {
        const selectors = [...source.matchAll(new RegExp(`querySelectorAll\\('\\.${tile}([^']*)'\\)`, "g"))].map((m) => m[1]);
        expect(selectors.length).toBeGreaterThan(0);
        for (const suffix of selectors) {
            expect(suffix).toContain("[data-id]");
        }
    });

    test("a skeleton tile carries the base class but no data-id", () => {
        // Both halves matter: sharing the base class is why the filter is
        // needed, and the missing data-id is what the filter keys on.
        const skeleton = render();
        expect(skeleton.classList.contains(tile)).toBe(true);
        expect(skeleton.hasAttribute("data-id")).toBe(false);
    });
});
