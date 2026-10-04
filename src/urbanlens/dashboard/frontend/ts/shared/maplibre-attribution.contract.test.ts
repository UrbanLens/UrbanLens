/**
 * MapLibre 5.x's own attribution control runs each source's attribution through `DOM.sanitize`, which skips the
 * attribute after each one it removes (GHSA-jrc7-96c5-q579, fixed only in 6.4.1). While the vendored build stays on
 * 5.x (`services/core/vendor_assets.py`), no MapLibre map may show that control.
 */

import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const TS_ROOT = join(import.meta.dir, "..");
const TEMPLATES_ROOT = join(import.meta.dir, "../../../templates/dashboard");
const LEAFLET_BRIDGE = join(import.meta.dir, "../../../../../../node_modules/@maplibre/maplibre-gl-leaflet/leaflet-maplibre-gl.js");
const MAP_CONSTRUCTOR = /new\s+(?:window\.)?(?:gl|maplibregl|maplibre_gl)\.Map\s*\(/g;

function sourceFiles(directory: string, extension: string): string[] {
    const found: string[] = [];
    for (const entry of readdirSync(directory)) {
        const path = join(directory, entry);
        if (statSync(path).isDirectory()) found.push(...sourceFiles(path, extension));
        else if (entry.endsWith(extension) && !entry.endsWith(`.test${extension}`)) found.push(path);
    }
    return found;
}

/** `source` without its comments, which quote constructor calls in prose. */
function code(source: string): string {
    return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|\s)\/\/[^\n]*/g, "$1");
}

/** The argument list of the call whose opening parenthesis ends at `start`. */
function callArguments(source: string, start: number): string {
    let depth = 1;
    for (let index = start; index < source.length; index++) {
        if (source[index] === "(") depth++;
        else if (source[index] === ")" && --depth === 0) return source.slice(start, index);
    }
    return source.slice(start);
}

function mapConstructions(): { path: string; options: string }[] {
    const calls: { path: string; options: string }[] = [];
    for (const path of [...sourceFiles(TS_ROOT, ".ts"), ...sourceFiles(TEMPLATES_ROOT, ".html")]) {
        const source = code(readFileSync(path, "utf8"));
        for (const match of source.matchAll(MAP_CONSTRUCTOR)) {
            calls.push({ path, options: callArguments(source, (match.index ?? 0) + match[0].length) });
        }
    }
    return calls;
}

describe("MapLibre's attribution control", () => {
    const constructions = mapConstructions();

    test("the scan finds the maps it is meant to guard", () => {
        expect(constructions.length).toBeGreaterThan(0);
    });

    test("the scan would catch a map built with it, and not one quoted in a comment", () => {
        const source = code("// new gl.Map({ attributionControl: false })\nconst map = new maplibregl.Map({ container, style: styleFor(kind) });");
        const calls = [...source.matchAll(MAP_CONSTRUCTOR)].map((match) => callArguments(source, (match.index ?? 0) + match[0].length));
        expect(calls).toEqual(["{ container, style: styleFor(kind) }"]);
    });

    test("every MapLibre map is built without it", () => {
        const enabled = constructions.filter(({ options }) => !/attributionControl\s*:\s*false/.test(options)).map(({ path }) => path);
        expect(enabled).toEqual([]);
    });

    test("no page adds it by hand", () => {
        const added = [...sourceFiles(TS_ROOT, ".ts"), ...sourceFiles(TEMPLATES_ROOT, ".html")].filter((path) => code(readFileSync(path, "utf8")).includes("AttributionControl"));
        expect(added).toEqual([]);
    });

    test("the Leaflet bridge builds its MapLibre map without it", () => {
        const bridge = readFileSync(LEAFLET_BRIDGE, "utf8");
        const initGL = bridge.slice(bridge.indexOf("_initGL"), bridge.indexOf("new maplibre_gl.Map"));
        expect(initGL).toMatch(/attributionControl:\s*false/);
    });
});
