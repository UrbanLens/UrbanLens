/**
 * The home page's "Recently viewed" widgets read localStorage keys that other pages write. Each side spells its key
 * separately, so this ties them together: the pins widget read an id-keyed list for two weeks after the map page
 * started writing a uuid-keyed one.
 */

import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const TS = join(import.meta.dir, "..");
const TEMPLATES = join(import.meta.dir, "../../../templates/dashboard");
const read = (path: string): string => readFileSync(path, "utf8");

function widgetKey(template: string): string {
    const match = read(join(TEMPLATES, "partials/home", template)).match(/data-recent-key="([^"]+)"/);
    if (!match?.[1]) throw new Error(`${template} names no data-recent-key`);
    return match[1];
}

test("the recently-viewed pins widget reads the key the map page writes", () => {
    const mapPage = read(join(TS, "entries/map-page.ts"));
    expect(mapPage).toContain('recentPinsKey: "ul_recent_pins_v1_" + _PROFILE_UUID');
    expect(mapPage).toContain("const _PROFILE_UUID = MAP_CFG.profileUuid;");
    expect(widgetKey("_widget_recently_viewed_pins.html")).toBe("ul_recent_pins_v1_{{ profile.uuid }}");
});

test("the recently-viewed wikis widget reads the key the wiki page writes", () => {
    const wikiPage = read(join(TEMPLATES, "pages/location/wiki.html"));
    expect(wikiPage).toContain('data-recent-key="ul_recent_wikis_v1_{{ request.user.profile.id }}"');
    expect(widgetKey("_widget_recently_viewed_wikis.html")).toBe("ul_recent_wikis_v1_{{ profile.id }}");
});
