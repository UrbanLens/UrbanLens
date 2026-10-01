/**
 * The site footer credits whichever tiles are currently drawn, for every map on the site.
 *
 * Two things have to hold and neither is visible from the other side: the writer has to survive
 * being called before the footer exists (several maps are built by an inline script that runs
 * further up the page than the footer include), and the footer has to keep its own links out of
 * the element the writer overwrites.
 */
import { afterEach, describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { setAttribution } from "./map-layers";

const FOOTER_TEMPLATE = join(import.meta.dir, "../../../templates/dashboard/partials/layout/footer.html");
const footerTemplate = readFileSync(FOOTER_TEMPLATE, "utf8");

/** The id the template gives the attribution slot - read from the template so the two cannot drift. */
const attributionId = footerTemplate.match(/<span class="page-footer__attribution-text" id="([^"]+)"/)?.[1];

/** The template's attribution slot, without the rest of the page around it. */
function makeFooterSlot(): HTMLElement {
    const el = document.createElement("span");
    el.id = attributionId!;
    document.body.appendChild(el);
    return el;
}

function setReadyState(value: string): void {
    Object.defineProperty(document, "readyState", { value, configurable: true });
}

afterEach(() => {
    setReadyState("complete");
    // Flushes any attribution still waiting, so one test's deferral cannot decide another's.
    document.dispatchEvent(new Event("DOMContentLoaded"));
    document.querySelectorAll("span[id]").forEach((el) => el.remove());
});

describe("setAttribution", () => {
    test("credits the tiles in the footer", () => {
        const slot = makeFooterSlot();

        setAttribution("© OSM · CARTO · Leaflet");

        expect(slot.textContent).toBe("© OSM · CARTO · Leaflet");
    });

    /**
     * A map built by an inline script in the page body reports its first attribution before the
     * footer below it has been parsed. That first one is the only attribution most of these pages
     * ever report, so losing it means the tiles are never credited at all.
     */
    test("credits tiles a map reported before the footer was parsed", () => {
        setReadyState("loading");

        setAttribution("© Esri");
        const slot = makeFooterSlot();
        document.dispatchEvent(new Event("DOMContentLoaded"));

        expect(slot.textContent).toBe("© Esri");
    });

    test("shows the layer that ended up drawn, not the one asked for first", () => {
        setReadyState("loading");

        setAttribution("© OSM · CARTO");
        setAttribution("© Esri");
        const slot = makeFooterSlot();
        document.dispatchEvent(new Event("DOMContentLoaded"));

        expect(slot.textContent).toBe("© Esri");
    });

    /** Maps live on pages with no footer at all (dialogs, the floorplan editor's own chrome). */
    test("is a no-op on a page that has no footer", () => {
        expect(() => setAttribution("© Esri")).not.toThrow();
    });
});

describe("the footer template", () => {
    test("names the slot the writer looks for", () => {
        const slot = makeFooterSlot();

        setAttribution("© OpenTopoMap");

        expect(attributionId).toBeDefined();
        expect(slot.textContent).toBe("© OpenTopoMap");
    });

    /**
     * The writer replaces the slot's entire text. Terms, Privacy and the rest living inside it
     * meant the first layer switch deleted the site's policy links.
     */
    test("keeps its own links outside the slot the writer overwrites", () => {
        const slot = footerTemplate.match(/<span class="page-footer__attribution-text"[^>]*>([\s\S]*?)<\/span>/)?.[1];

        expect(slot).toBeDefined();
        expect(slot).not.toContain("<a ");
        for (const link of ["thanks", "terms", "privacy"]) expect(footerTemplate).toContain(`{% url '${link}' %}`);
    });
});
