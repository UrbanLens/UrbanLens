/**
 * The MapLibre this site builds for itself (`entries-classic/maplibre-gl.ts`), in a real browser.
 *
 * MapLibre 6 ships only ES modules, and a bundled copy cannot find its own worker, which fails at the first map rather
 * than at build time. Nothing short of a browser starting that worker shows the build is right.
 */

import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { existsSync } from "node:fs";
import { join } from "node:path";

import { version as packageVersion } from "maplibre-gl/package.json";
import { type Browser, type Page, chromium } from "playwright";

// Bun.serve does not recognise a happy-dom Response; see floorplan-editor.test.ts.
GlobalRegistrator.unregister();

const ROOT = join(import.meta.dir, "../../../../..");
const STATIC_DIR = join(ROOT, "src/urbanlens/dashboard/frontend/static");
const BUNDLE = join(STATIC_DIR, "dashboard/js/maplibre-gl.js");
const LEAFLET_BRIDGE = join(ROOT, "node_modules/@maplibre/maplibre-gl-leaflet/leaflet-maplibre-gl.js");

/** These drive the built bundle, so they need `bun run build` to have run. */
const BUILT = existsSync(BUNDLE);

/**
 * The site's own `script-src`, `worker-src` and `connect-src` as far as these pages need them (settings/base.py;
 * `test_security_headers.py` holds the site to `worker-src 'self' blob:`). `connect-src 'self'` is what lets a test
 * see whether the worker is held to this policy.
 */
const POLICY = "default-src 'self'; script-src 'self' https://unpkg.com; style-src 'self' 'unsafe-inline' https://unpkg.com; img-src 'self' data: blob: https://unpkg.com; worker-src 'self' blob:; connect-src 'self'";

const PAGE = `<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="/dashboard/js/maplibre-gl.js"></script>
<script src="/leaflet-maplibre-gl.js"></script>
<script src="/csp-log.js"></script>
</head><body><div id="map" style="width:400px;height:300px"></div></body></html>`;

/** Records every policy violation the page raises; a classic file because the policy refuses inline script. */
const CSP_LOG = `window.cspViolations = [];
document.addEventListener("securitypolicyviolation", (e) => window.cspViolations.push(e.effectiveDirective + " " + e.blockedURI));`;

const POINT = { type: "Feature", properties: { name: "here" }, geometry: { type: "Point", coordinates: [0, 0] } };

let browser: Browser;
let page: Page;
let server: ReturnType<typeof Bun.serve>;
/** Another origin, which the policy above does not admit. */
let elsewhere: ReturnType<typeof Bun.serve>;
let elsewhereHits = 0;

beforeAll(async () => {
    if (!BUILT) return;
    // Chromium's libraries live under ~/browserlibs on hosts with no root; see floorplan-editor.test.ts.
    const libs = join(process.env.HOME || "", "browserlibs/root/usr/lib/x86_64-linux-gnu");
    process.env.LD_LIBRARY_PATH = process.env.LD_LIBRARY_PATH ? `${process.env.LD_LIBRARY_PATH}:${libs}` : libs;
    browser = await chromium.launch({ args: ["--no-sandbox"] });
    elsewhere = Bun.serve({
        port: 0,
        fetch() {
            elsewhereHits += 1;
            return Response.json(POINT, { headers: { "access-control-allow-origin": "*" } });
        },
    });
    server = Bun.serve({
        port: 0,
        async fetch(request) {
            const path = new URL(request.url).pathname;
            if (path === "/") return new Response(PAGE, { headers: { "content-type": "text/html", "content-security-policy": POLICY } });
            if (path === "/csp-log.js") return new Response(CSP_LOG, { headers: { "content-type": "text/javascript" } });
            if (path === "/point.json") return Response.json(POINT);
            if (path === "/leaflet-maplibre-gl.js") return new Response(Bun.file(LEAFLET_BRIDGE), { headers: { "content-type": "text/javascript" } });
            const file = Bun.file(join(STATIC_DIR, path.replace(/^\//, "")));
            if (!(await file.exists())) return new Response("not found", { status: 404 });
            return new Response(file, { headers: { "content-type": "text/javascript" } });
        },
    });
    page = await browser.newPage();
    await page.goto(`http://127.0.0.1:${server.port}/`, { waitUntil: "load" });
});

afterAll(async () => {
    await browser?.close();
    server?.stop(true);
    elsewhere?.stop(true);
});

/** Builds a map drawing the GeoJSON at `data`, and reports how many features its worker parsed or why it failed. */
async function drawGeojson(data: string): Promise<{ features?: number; error?: string }> {
    return page.evaluate(async (url) => {
        document.getElementById("map")?.replaceChildren();
        const map = new window.maplibregl.Map({
            container: "map",
            style: { version: 8, sources: { point: { type: "geojson", data: url } }, layers: [{ id: "point", type: "circle", source: "point" }] },
            center: [0, 0],
            zoom: 2,
            attributionControl: false,
        });
        try {
            return await new Promise<{ features?: number; error?: string }>((resolve) => {
                map.on("error", (event) => resolve({ error: String(event.error?.message ?? event.error) }));
                map.once("idle", () => resolve({ features: map.querySourceFeatures("point").length }));
                setTimeout(() => resolve({ error: "the map never went idle" }), 15_000);
            });
        } finally {
            map.remove();
        }
    }, data);
}

async function violations(): Promise<string[]> {
    return page.evaluate(() => (window as unknown as { cspViolations: string[] }).cspViolations);
}

describe.skipIf(!BUILT)("the MapLibre bundle in a browser", () => {
    test("defines the maplibregl global, at the release package.json pins", async () => {
        expect(await page.evaluate(() => window.maplibregl.getVersion())).toBe(packageVersion);
    });

    test("this browser can draw at all, or the rest of this file proves nothing", async () => {
        expect(await page.evaluate(() => document.createElement("canvas").getContext("webgl2") !== null)).toBe(true);
    });

    test("starts its worker under the site's policy, and the worker parses a source", async () => {
        const drawn = await drawGeojson("/point.json");

        expect(drawn).toEqual({ features: expect.any(Number) });
        expect(drawn.features).toBeGreaterThan(0);
        expect(await violations()).toEqual([]);
    });

    test("holds the worker to the page's connect-src, as MapLibre 5's blob: worker was", async () => {
        // Started from its own URL, the worker would answer only to the headers its file was served with - none here.
        const hitsBefore = elsewhereHits;

        const drawn = await drawGeojson(`http://127.0.0.1:${elsewhere.port}/point.json`);

        expect(drawn.error).toContain("Failed to fetch");
        expect(elsewhereHits).toBe(hitsBefore);
    });

    test("feeds the Leaflet bridge, which reads the global once as it loads", async () => {
        const drawn = await page.evaluate(async () => {
            const leaflet = (window as unknown as { L: typeof import("leaflet") & { maplibreGL(options: object): import("leaflet").Layer & { getMaplibreMap(): import("maplibre-gl").Map } } }).L;
            document.getElementById("map")?.replaceChildren();
            const map = leaflet.map("map").setView([0, 0], 3);
            const layer = leaflet.maplibreGL({ style: { version: 8, sources: { point: { type: "geojson", data: "/point.json" } }, layers: [{ id: "point", type: "circle", source: "point" }] } }).addTo(map);
            const gl = layer.getMaplibreMap();
            await new Promise<void>((resolve) => {
                if (gl.loaded()) resolve();
                gl.once("idle", () => resolve());
                setTimeout(resolve, 15_000);
            });
            const result = { features: gl.querySourceFeatures("point").length, canvas: document.querySelector("#map canvas.leaflet-image-layer") !== null };
            map.remove();
            return result;
        });

        expect(drawn.canvas).toBe(true);
        expect(drawn.features).toBeGreaterThan(0);
        expect(await violations()).toEqual([]);
    });
});
