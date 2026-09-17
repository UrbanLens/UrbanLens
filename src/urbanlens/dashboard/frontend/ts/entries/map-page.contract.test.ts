/**
 * P92 regression guard: the main map's cluster group must come from the
 * shared factory (see shared/map-clusters.test.ts for the factory's own
 * behavioural coverage), not a hand-rolled L.markerClusterGroup() with its
 * own copy of the numbered-badge markup that can silently drift from every
 * other map's. This entry has heavy module-scope side effects (it reads
 * #map-page-config and constructs a live Leaflet map on import), so it is
 * checked as text rather than imported and executed in a test environment.
 */
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const ENTRY = join(import.meta.dir, "map-page.ts");
const source = readFileSync(ENTRY, "utf8");

describe("map-page's main cluster group", () => {
    test("imports the shared cluster factory", () => {
        expect(source).toMatch(/import\s*\{[^}]*\bcreatePinClusterGroup\b[^}]*\}\s*from\s*"\.\.\/shared\/map-clusters"/);
    });

    test("builds the main clusterGroup through createPinClusterGroup, not a bare L.markerClusterGroup", () => {
        const built = source.match(/const clusterGroup = createPinClusterGroup\(([\s\S]*?)\n\);/);
        expect(built, "clusterGroup is no longer built via createPinClusterGroup(...)").not.toBeNull();

        // The main map's real differences from the shared defaults - its own
        // radius policy and the chunked-loading options a multi-thousand-pin
        // account needs - must still reach the factory as overrides.
        const options = built?.[1] ?? "";
        expect(options).toContain("maxClusterRadius");
        expect(options).toContain("chunkedLoading: true");

        // No second, driftable copy of the cluster group construction anywhere
        // else in the file (comment lines mentioning it, like the one right
        // above the real call, don't count).
        const codeOnly = source
            .split("\n")
            .filter((line) => !line.trim().startsWith("//"))
            .join("\n");
        const directCalls = codeOnly.match(/\bL\.markerClusterGroup\(/g) ?? [];
        expect(directCalls).toHaveLength(0);
    });
});
