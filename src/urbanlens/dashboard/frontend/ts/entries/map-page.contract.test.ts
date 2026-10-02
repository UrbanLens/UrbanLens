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

describe("map-page's background refresh and poll", () => {
    const codeOnly = source
        .split("\n")
        .filter((line) => !line.trim().startsWith("//"))
        .join("\n");

    test("the full refresh every caller reaches is the single-flight wrapper", () => {
        expect(codeOnly).toMatch(/const _refreshAllPins = singleFlight\(_runFullRefresh\);/);
        expect(codeOnly).toMatch(/window\._refreshAllPins = _refreshAllPins;/);
        // The definition and the wrap: nothing calls the unguarded run directly.
        expect(codeOnly.match(/\b_runFullRefresh\b/g)).toHaveLength(2);
    });

    test("the pin poll is a startPoller tied to the map container, not a bare interval", () => {
        expect(codeOnly).toMatch(/startPoller\(_pollForUpdates, \{[^}]*element: map\.getContainer\(\)/);
        expect(codeOnly).not.toMatch(/\bsetInterval\(/);
    });
});

/** The text of the function declared as ``function <name>(``, up to its closing brace at the same indent. */
function functionBody(name: string): string {
    const start = source.search(new RegExp(`^( *)(async )?function ${name}\\(`, "m"));
    expect(start, `${name} is no longer declared as a function`).toBeGreaterThan(-1);
    const indent = /^ */.exec(source.slice(start))![0];
    const end = source.indexOf(`\n${indent}}\n`, start);
    return source.slice(start, end);
}

/** The ``.catch(...)`` handler at the end of a request chain, which is what runs when the request fails. */
function failureHandler(body: string): string {
    const at = body.lastIndexOf(".catch(");
    expect(at, "the request has no failure handler").toBeGreaterThan(-1);
    return body.slice(at);
}

describe("map-page's requests leave their controls usable when they fail", () => {
    test("a failed bulk-delete undo re-enables the toast's Undo button for another try", () => {
        expect(failureHandler(functionBody("_undoBulkDelete"))).toMatch(/btn\.disabled = false/);
    });

    test("a failed location switch or merge puts the button's own label back, not 'Switching...'", () => {
        const handler = failureHandler(functionBody("_linkPin"));
        expect(handler).toMatch(/btn\.disabled = false/);
        expect(handler).toMatch(/btn\.textContent = /);
    });

    test("adding pins to a list asks its 409 question without the fetch net's error toast, and still reports a network failure", () => {
        const body = functionBody("addPinsToList");
        expect(body).toMatch(/__ulReported: true/);
        expect(failureHandler(body)).toMatch(/toastr\.error\(/);
    });
});
