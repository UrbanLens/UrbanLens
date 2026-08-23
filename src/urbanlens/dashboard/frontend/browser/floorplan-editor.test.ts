/**
 * Gesture tests for the floorplan editor, in a real browser.
 *
 * Everything else about the editor can be tested as pure functions, and is.
 * Dragging cannot: it is the point where this code meets Leaflet, pointer
 * capture, and a DOM that rebuilds itself mid-gesture, and every serious defect
 * in the drag work came from that seam rather than from the arithmetic either
 * side of it. Two of them - a drag that ended after one frame because render()
 * destroys the element it was bound to, and a drag that froze because it
 * snapped to the wall it was itself dragging - are invisible to a unit test by
 * construction.
 *
 * The page is a harness rather than the Django template: the template needs a
 * server, and what is under test is the bundle's behaviour, not Django's
 * rendering. The bundle is the real built artifact.
 *
 * Run with `bun run test:browser`. It lives outside `frontend/ts/` on purpose:
 * bunfig.toml preloads happy-dom for everything under there, and unregistering
 * it - which this file must do, since a simulated DOM and a real browser cannot
 * both own the globals - would strip the DOM from every other test in the same
 * process. A separate directory means a separate `bun test` invocation.
 *
 * Requires `bun run build` first, and a chromium Playwright can launch; see
 * bin/browser_libs.sh for the shared libraries this host needs.
 */

import { GlobalRegistrator } from "@happy-dom/global-registrator";
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { existsSync } from "node:fs";
import { join } from "node:path";

import { type Browser, type Page, chromium } from "playwright";

// bunfig.toml preloads happy-dom for every test file, which is right for the
// rest of the suite and wrong here: this file drives a *real* browser, and the
// simulated globals it installs are not inert. Bun.serve handed a happy-dom
// Response does not recognise it and quietly serves its own welcome page
// instead, so the harness never loads and the editor looks broken.
GlobalRegistrator.unregister();

const ROOT = join(import.meta.dir, "../../../../..");
const STATIC_DIR = join(ROOT, "src/urbanlens/dashboard/frontend/static");
const BUNDLE = join(STATIC_DIR, "dashboard/js/floorplan-editor.js");

/**
 * These drive the built bundle, so they need `bun run build` to have run.
 * Skipped rather than failed without it: a missing build is a setup gap, and a
 * red suite that means "you did not build" trains people to ignore red suites.
 */
const BUILT = existsSync(BUNDLE);

/**
 * Test-controlled save behaviour.
 *
 * A conflict is a server state, not a client one, so the only honest way to
 * reach it is for the server to answer the way a real one would when another
 * tab has saved first.
 */
const saves = { conflict: false, attempts: 0 };

/** A minimal page carrying every element boot() reaches for. */
const HARNESS = `<!doctype html>
<html><head>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<!-- The site's own compiled stylesheet, so layout questions can be asked here
     at all: without it every class in the markup is inert. -->
<link rel="stylesheet" href="/dashboard/style.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet-rotate@0.2.8/dist/leaflet-rotate-src.js"></script>
<style>
/* This block is the harness's own layout, not the site's - a test that asks
   where something sits at a given viewport width is answered by these rules
   rather than by _floorplans.scss. Ask structural questions here (what is
   inside what), and treat any measurement that disagrees with the real page
   as this fixture talking. */
html,body{margin:0}
#floorplan-map{width:900px;height:640px}
/* Out of flow and fixed: in the real page the sidebar is a fixed-width flex
   column, but in bare markup it grows as the editor fills it, reflowing the
   page and making Leaflet re-project the plan at a different scale. That reads
   as geometry moving when nothing has moved at all. */
.floorplan-sidebar{position:fixed;top:0;right:0;width:320px;height:100vh;overflow:auto}
</style>
</head><body>
<div id="floorplan-origin-banner" hidden></div>
<div class="floorplan-editor"><div class="floorplan-editor__stage"><div class="floorplan-map-shell">
  <div id="floorplan-tools" class="map-buttons"><div class="map-buttons-content">
    <button class="map-btn-icon is-active" data-tool="select"></button>
    <button class="map-btn-icon" data-tool="box"></button>
    <button class="map-btn-icon" data-tool="rotate"></button>
    <button class="map-btn-icon" data-tool="wall"></button>
    <button class="map-btn-icon" data-tool="opening"></button>
    <button class="map-btn-icon" data-tool="room"></button>
    <button class="map-btn-icon" data-tool="marker"></button>
  </div></div>
  <div id="floorplan-map" tabindex="0" role="application" aria-label="Floorplan canvas"
       data-json-url="/json" data-save-url="/save" data-publish-url="/publish"
       data-lat="41.733" data-lng="-73.928"></div>
  <div id="floorplan-tool-options" hidden></div>
  <div class="floorplan-canvas-controls">
    <button id="floorplan-undo" disabled></button>
    <button id="floorplan-redo" disabled></button>
  </div>
  <div class="floorplan-canvas-floors">
    <span class="floorplan-field__label">Floors</span>
    <div id="floorplan-floors" class="floorplan-floors"></div>
  </div>
  <div class="map-bottom-controls"><div id="floorplan-layers"></div></div>
  <div id="floorplan-empty"><button id="floorplan-start-outline"></button><button id="floorplan-start-rectangle"></button></div>
</div><p id="floorplan-hint"></p><p id="floorplan-live"></p></div>
<aside class="floorplan-sidebar">
  <span id="floorplan-save-status"></span>
  <button id="floorplan-retry-save" hidden></button>
  <button id="floorplan-reload" hidden></button>
  <button id="floorplan-more-toggle"></button><div id="floorplan-more-list" hidden>
    <button id="floorplan-save-version"></button><button id="floorplan-publish"></button></div>
  <div id="floorplan-floor-fields"></div>
  <div id="floorplan-form"></div>
  <div id="floorplan-marker-appearance" hidden>
    <input id="icon-value-floorplan-marker">
    <div class="color-picker" id="color-picker-floorplan-marker">
      <input type="hidden" id="color-value-floorplan-marker" value="">
      <button type="button" class="color-swatch" data-color="#e53935" style="background-color:#e53935"
              onclick="pickColor('color-picker-floorplan-marker','color-value-floorplan-marker','#e53935',this)"></button>
      <button type="button" class="color-swatch color-clear selected" data-color=""
              onclick="pickColor('color-picker-floorplan-marker','color-value-floorplan-marker','',this)"><i class="material-symbols-outlined">block</i></button>
    </div>
  </div>
  <input id="floorplan-name"><input id="floorplan-valid-from" type="date">
  <div id="floorplan-versions" hidden></div>
</aside></div>
<script id="floorplan-outline" type="application/json">[]</script>
<script id="floorplan-overlays" type="application/json">[]</script>
<script id="floorplan-labels" type="application/json">[]</script>
<script id="floorplan-photos" type="application/json">[]</script>
<script type="module" src="/dashboard/js/floorplan-editor.js"></script>
</body></html>`;

/** Where the middle of the plan sits on screen. */
async function planCentre(): Promise<{ x: number; y: number }> {
    const point = await page.evaluate(() => {
        const nodes = [...document.querySelectorAll(".floorplan-wall")];
        let top = Infinity;
        let left = Infinity;
        let bottom = -Infinity;
        let right = -Infinity;
        for (const node of nodes) {
            const rect = node.getBoundingClientRect();
            top = Math.min(top, rect.top);
            left = Math.min(left, rect.left);
            bottom = Math.max(bottom, rect.bottom);
            right = Math.max(right, rect.right);
        }
        return { x: (left + right) / 2, y: (top + bottom) / 2 };
    });
    return point;
}

/** A four-wall square, 10m on a side, as the editor's own document shape. */
/**
 * A grid of rooms, for asking what a real survey costs to redraw.
 *
 * Args:
 *     cells: Rooms per side. 12 gives 312 walls and 144 faces.
 */
function gridPlan(cells = 12): unknown {
    const step = 3;
    const walls: unknown[] = [];
    for (let line = 0; line <= cells; line++) {
        for (let span = 0; span < cells; span++) {
            const edge = line === 0 || line === cells;
            const kind = edge ? "exterior" : "interior";
            walls.push({ kind, thickness: "normal", ax: line * step, ay: span * step, bx: line * step, by: (span + 1) * step, openings: [] });
            walls.push({ kind, thickness: "normal", ax: span * step, ay: line * step, bx: (span + 1) * step, by: line * step, openings: [] });
        }
    }
    return {
        uuid: "plan-big",
        name: "grid",
        valid_from: null,
        origin: "local",
        plan_origin: { lat: 41.733, lng: -73.928 },
        rotation_degrees: 0,
        floors: [{ uuid: "floor-1", level: 0, designation: "", name: "Ground", walls, rooms: [], markers: [] }],
    };
}

/** A square building with a closet partitioned off its north-west corner. */
function closetPlan(): unknown {
    const wall = (kind: string, ax: number, ay: number, bx: number, by: number) => ({ kind, thickness: "normal", ax, ay, bx, by, openings: [] });
    return {
        uuid: "plan-closet",
        name: "closet",
        valid_from: null,
        origin: "local",
        plan_origin: { lat: 41.733, lng: -73.928 },
        rotation_degrees: 0,
        floors: [
            {
                uuid: "floor-1",
                level: 0,
                designation: "",
                name: "Ground",
                walls: [
                    wall("exterior", 0, 0, 10, 0),
                    wall("exterior", 10, 0, 10, 10),
                    wall("exterior", 10, 10, 0, 10),
                    wall("exterior", 0, 10, 0, 0),
                    // The closet: a partition across, and one down to meet it.
                    wall("interior", 0, 4, 4, 4),
                    wall("interior", 4, 4, 4, 0),
                ],
                rooms: [{ name: "Closet", x: 2, y: 2 }],
                markers: [],
            },
        ],
    };
}

/** A square building with a free-standing box inside it, touching nothing. */
function islandPlan(): unknown {
    const wall = (kind: string, ax: number, ay: number, bx: number, by: number) => ({ kind, thickness: "normal", ax, ay, bx, by, openings: [] });
    return {
        uuid: "plan-island",
        name: "island",
        valid_from: null,
        origin: "local",
        plan_origin: { lat: 41.733, lng: -73.928 },
        rotation_degrees: 0,
        floors: [
            {
                uuid: "floor-1",
                level: 0,
                designation: "",
                name: "Ground",
                walls: [
                    wall("exterior", 0, 0, 10, 0),
                    wall("exterior", 10, 0, 10, 10),
                    wall("exterior", 10, 10, 0, 10),
                    wall("exterior", 0, 10, 0, 0),
                    wall("interior", 3, 3, 6, 3),
                    wall("interior", 6, 3, 6, 6),
                    wall("interior", 6, 6, 3, 6),
                    wall("interior", 3, 6, 3, 3),
                ],
                rooms: [{ name: "Island", x: 4.5, y: 4.5 }],
                markers: [],
            },
        ],
    };
}

function squarePlan(): unknown {
    const corners = [
        [0, 0],
        [10, 0],
        [10, 10],
        [0, 10],
    ];
    return {
        uuid: "plan-1",
        name: "harness",
        valid_from: null,
        origin: "local",
        plan_origin: { lat: 41.733, lng: -73.928 },
        rotation_degrees: 0,
        floors: [
            {
                uuid: "floor-1",
                level: 0,
                designation: "",
                name: "",
                walls: corners.map((corner, index) => ({
                    uuid: `wall-${index}`,
                    // All exterior: this is a building outline, which is what
                    // makes it a shell rather than a room.
                    kind: "exterior",
                    thickness: "normal",
                    ax: corner[0],
                    ay: corner[1],
                    bx: corners[(index + 1) % 4]?.[0],
                    by: corners[(index + 1) % 4]?.[1],
                    openings: [],
                })),
                rooms: [],
                markers: [],
            },
        ],
    };
}

let browser: Browser;
let page: Page;
let server: ReturnType<typeof Bun.serve>;

/**
 * Load the harness with the real bundle and a stubbed server.
 *
 * Served over HTTP rather than injected: the bundle is a module that imports a
 * chunk by relative URL, which cannot resolve without a real origin.
 */
async function openEditor(viewport = { width: 1200, height: 800 }, hasTouch = false, plan: "square" | "grid" | "closet" | "island" = "square"): Promise<void> {
    page = await browser.newPage({ viewport, hasTouch });
    page.on("pageerror", (error) => console.error("PAGEERROR", String(error).slice(0, 300)));
    page.on("console", (message) => {
        if (message.type() === "error") console.error("browser console:", message.text().slice(0, 200));
    });
    await page.goto(`http://127.0.0.1:${server.port}/${plan === "square" ? "" : `?plan=${plan}`}`, { waitUntil: "load" });
    // "attached", not the default "visible": a straight wall is an SVG line
    // whose bounding box has zero height, which Playwright reads as invisible.
    await page.waitForSelector(".floorplan-wall", { state: "attached", timeout: 20000 });
    // Walls existing is not the same as the page having settled - Leaflet is
    // still placing panes on the frame they appear, and a measurement taken
    // then reads the layout moving as the plan moving.
    await settle();
}

/**
 * Wait until the plan stops moving on screen.
 *
 * The editor fits the view to the plan once it loads, and Leaflet animates
 * that. Measuring during the animation reads the zoom settling as the geometry
 * changing, which is the difference between a test that fails once in ten runs
 * and one that means something.
 */
async function settle(): Promise<void> {
    let previous = "";
    for (let attempt = 0; attempt < 40; attempt++) {
        await page.evaluate(() => new Promise<void>((done) => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
        const current = await page.evaluate(() => {
            const nodes = [...document.querySelectorAll(".floorplan-wall")];
            return nodes.map((node) => {
                const rect = node.getBoundingClientRect();
                return `${Math.round(rect.left)},${Math.round(rect.top)},${Math.round(rect.width)},${Math.round(rect.height)}`;
            }).join("|");
        });
        if (current && current === previous) return;
        previous = current;
    }
}

/**
 * The screen extent of every wall on the floor, and a point on the topmost one.
 *
 * Measured as a union rather than per element, and read in the page rather than
 * through Playwright's boundingBox(). Both matter: render() destroys and
 * rebuilds every layer on each frame, so "the first .floorplan-wall" is not the
 * same wall before and after a gesture - measuring one that way reports a plain
 * click as a 70px move - and a straight wall is a zero-height line, which
 * Playwright treats as invisible and refuses to measure at all.
 */
async function planExtent(): Promise<{ top: number; left: number; bottom: number; width: number; height: number; grab: { x: number; y: number } }> {
    const measured = await page.evaluate(() => {
        const nodes = [...document.querySelectorAll(".floorplan-wall")];
        if (!nodes.length) return null;
        // Relative to the map, not the viewport: selecting something fills the
        // sidebar, which reflows the page and slides the map sideways. That is
        // the harness moving, not the plan.
        const frame = document.getElementById("floorplan-map")?.getBoundingClientRect();
        const originX = frame?.left ?? 0;
        const originY = frame?.top ?? 0;
        let top = Infinity;
        let left = Infinity;
        let bottom = -Infinity;
        let right = -Infinity;
        let highest: DOMRect | null = null;
        for (const node of nodes) {
            const rect = node.getBoundingClientRect();
            top = Math.min(top, rect.top - originY);
            left = Math.min(left, rect.left - originX);
            bottom = Math.max(bottom, rect.bottom - originY);
            right = Math.max(right, rect.right - originX);
            // The topmost horizontal run: a wide, flat rect near the top.
            if (rect.width > rect.height && (!highest || rect.top < highest.top)) highest = rect;
        }
        const target = highest ?? nodes[0]!.getBoundingClientRect();
        return { top, left, bottom, width: right - left, height: bottom - top, grab: { x: target.left + target.width / 2, y: target.top + target.height / 2 } };
    });
    if (!measured) throw new Error("no walls are rendered");
    return measured;
}

beforeAll(async () => {
    if (!BUILT) return;
    // Chromium needs libraries this host has no root to install; they are
    // unpacked under ~/browserlibs instead. Set before launch so the browser
    // process inherits it.
    const libs = join(process.env.HOME || "", "browserlibs/root/usr/lib/x86_64-linux-gnu");
    process.env.LD_LIBRARY_PATH = process.env.LD_LIBRARY_PATH ? `${process.env.LD_LIBRARY_PATH}:${libs}` : libs;
    browser = await chromium.launch({ args: ["--no-sandbox", "--disable-gpu"] });
    server = Bun.serve({
        port: 0,
        // The plan is served here rather than intercepted with page.route: a
        // route that fails to match is indistinguishable from a 404, and the
        // editor is right to refuse to render a plan it could not fetch - which
        // makes a mis-typed glob look exactly like a broken editor.
        fetch(request) {
            const path = new URL(request.url).pathname;
            if (path === "/") {
                // The plan url is substituted rather than fixed, so a test can
                // ask for a big one without a second copy of the harness.
                const which = new URL(request.url).searchParams.get("plan");
                const page = which ? HARNESS.replace('data-json-url="/json"', `data-json-url="/json-${which}"`) : HARNESS;
                return new Response(page, { headers: { "content-type": "text/html" } });
            }
            if (path === "/json") return Response.json(squarePlan());
            if (path === "/json-grid") return Response.json(gridPlan());
            if (path === "/json-closet") return Response.json(closetPlan());
            if (path === "/json-island") return Response.json(islandPlan());
            if (path === "/save") {
                saves.attempts += 1;
                if (saves.conflict) {
                    return Response.json({ ok: false, error: "Someone else saved this plan while you were editing it.", stale: true }, { status: 409 });
                }
                return Response.json({ ok: true, floorplan: squarePlan() });
            }
            return new Response(Bun.file(join(STATIC_DIR, path.replace(/^\//, ""))));
        },
    });
});

afterAll(async () => {
    await browser?.close();
    server?.stop(true);
});

describe.skipIf(!BUILT)("floorplan editor in a browser", () => {
    test("the plan renders", async () => {
        await openEditor();
        expect(await page.locator(".floorplan-wall").count()).toBeGreaterThanOrEqual(4);
        expect(await page.locator(".floorplan-joint").count()).toBeGreaterThan(0);
        await page.close();
    });

    test("a mouse drag moves a wall, and keeps moving past the first frame", async () => {
        // The regression that motivates this file: binding the drag to the
        // layer's own element meant render() destroyed it on the first move,
        // so the wall travelled a few pixels and stopped.
        await openEditor();
        const before = await planExtent();
        await page.mouse.move(before.grab.x, before.grab.y);
        await page.mouse.down();
        for (let step = 1; step <= 10; step++) await page.mouse.move(before.grab.x, before.grab.y - step * 6);
        await page.mouse.up();

        const after = await planExtent();
        // The topmost wall moved up, so the plan's top edge did too.
        expect(before.top - after.top).toBeGreaterThan(30);
        await page.close();
    });

    test("a touch drag moves a wall", async () => {
        // Every drag was bound to mouse events, which a finger never emits, so
        // this is the whole of "the editor works on a phone".
        await openEditor();
        const before = await planExtent();
        const target = await page.evaluateHandle((point) => {
            const spot = point as { x: number; y: number };
            return document.elementFromPoint(spot.x, spot.y) ?? document.querySelector(".floorplan-wall");
        }, before.grab);
        await page.evaluate(
            ([element, start]) => {
                const node = element as Element;
                const from = start as { x: number; y: number };
                const fire = (type: string, x: number, y: number): void => {
                    node.dispatchEvent(new PointerEvent(type, { pointerId: 7, pointerType: "touch", isPrimary: true, bubbles: true, cancelable: true, clientX: x, clientY: y, buttons: type === "pointerup" ? 0 : 1 }));
                };
                fire("pointerdown", from.x, from.y);
                for (let step = 1; step <= 10; step++) {
                    window.dispatchEvent(new PointerEvent("pointermove", { pointerId: 7, pointerType: "touch", isPrimary: true, bubbles: true, clientX: from.x, clientY: from.y - step * 6, buttons: 1 }));
                }
                window.dispatchEvent(new PointerEvent("pointerup", { pointerId: 7, pointerType: "touch", isPrimary: true, bubbles: true, clientX: from.x, clientY: from.y - 60, buttons: 0 }));
            },
            [target, before.grab] as const,
        );

        const after = await planExtent();
        expect(before.top - after.top).toBeGreaterThan(30);
        await page.close();
    });

    test("a press that does not move selects instead of nudging", async () => {
        await openEditor();
        const before = await planExtent();
        await page.mouse.click(before.grab.x, before.grab.y);

        // Selecting a wall is what opens its form. Waited for rather than
        // asserted immediately: filling the sidebar reflows the page, and
        // measuring mid-reflow reads the harness moving as the plan moving.
        await page.waitForSelector("#floorplan-form h3", { state: "attached", timeout: 10000 });
        expect(await page.locator("#floorplan-form h3").count()).toBe(1);
        await settle();

        // The plan's own size, not its position on screen. Filling the sidebar
        // reflows the page and moves the map, which any position-based check
        // reports as the plan having moved; nudging a wall is what would change
        // the size of what the walls enclose.
        const after = await planExtent();
        expect(Math.abs(after.width - before.width)).toBeLessThan(1.5);
        expect(Math.abs(after.height - before.height)).toBeLessThan(1.5);
        await page.close();
    });

    test("undo takes back exactly one drag", async () => {
        await openEditor();
        const before = await planExtent();
        await page.mouse.move(before.grab.x, before.grab.y);
        await page.mouse.down();
        for (let step = 1; step <= 8; step++) await page.mouse.move(before.grab.x, before.grab.y - step * 8);
        await page.mouse.up();
        expect(await page.locator("#floorplan-undo").isDisabled()).toBe(false);
        expect((await planExtent()).top).toBeLessThan(before.top - 20);

        await page.keyboard.press("Control+z");

        const after = await planExtent();
        expect(Math.abs(after.top - before.top)).toBeLessThan(2);
        await page.close();
    });

    test("arming a tool changes the cursor, and Escape disarms it", async () => {
        await openEditor();
        await page.locator('[data-tool="rotate"]').click();
        expect(await page.locator("#floorplan-map").evaluate((el) => getComputedStyle(el).cursor)).toBe("grab");

        await page.keyboard.press("Escape");

        expect(await page.locator('[data-tool="select"]').getAttribute("aria-pressed")).toBe("true");
        await page.close();
    });

    test("clicking inside a bare outline does not turn the building into a room", async () => {
        // A region is derived from whatever encloses it, so an un-subdivided
        // outline encloses exactly as validly as a room - and used to become
        // one the moment anybody clicked the middle of the plan to look at
        // something.
        await openEditor();
        const centre = await planCentre();
        await page.mouse.click(centre.x, centre.y);
        await settle();

        expect(await page.locator(".floorplan-room-label").count()).toBe(0);
        expect(await page.locator("#floorplan-form h3").count()).toBe(0);
        await page.close();
    });

    test("the outline can still be named deliberately", async () => {
        await openEditor();
        const centre = await planCentre();
        await page.mouse.click(centre.x, centre.y, { button: "right" });
        await page.waitForSelector("text=Name this space", { timeout: 10000 });
        await page.locator("text=Name this space").click();
        await settle();

        expect(await page.locator("#floorplan-form h3").first().textContent()).toBe("Room");
        await page.close();
    });

    test("on a phone, undo and the floor switcher stay on the canvas", async () => {
        // They used to live in the sidebar, which stacks below the map under
        // 900px - so for the whole time anyone was drawing on a phone, the one
        // control you reach for after a mistake was off the bottom of the page.
        await openEditor({ width: 375, height: 812 });

        for (const selector of ["#floorplan-undo", "#floorplan-floors"]) {
            const placement = await page.evaluate((which) => {
                const node = document.querySelector(which as string);
                if (!node) return null;
                const rect = node.getBoundingClientRect();
                return {
                    // Structural, not positional: a control can be made to
                    // overlap the map with CSS while still living in a panel
                    // that scrolls away, which is what this is guarding against.
                    inShell: Boolean(node.closest(".floorplan-map-shell")),
                    rendered: rect.width > 0 && rect.height > 0,
                    onScreen: rect.top >= 0 && rect.bottom <= window.innerHeight,
                };
            }, selector);
            expect(placement, `${selector} is missing`).not.toBeNull();
            expect(placement!.inShell, `${selector} should live in the map shell`).toBe(true);
            expect(placement!.rendered, `${selector} should be rendered`).toBe(true);
            expect(placement!.onScreen, `${selector} should be reachable without scrolling`).toBe(true);
        }
        await page.close();
    });

    test("the floating controls do not sit on top of each other", async () => {
        // Six things float over this canvas - the tool pill, the tool options,
        // undo, the floor strip, the layers panel, and Leaflet's own zoom
        // buttons - positioned across three stylesheets, one of which is
        // Leaflet's. Overlap is the failure mode nobody notices until a button
        // cannot be pressed.
        for (const viewport of [
            { width: 375, height: 812 },
            { width: 1200, height: 800 },
        ]) {
            await openEditor(viewport);
            await page.locator('[data-tool="wall"]').click(); // give the options panel content
            await settle();

            const overlaps = await page.evaluate(() => {
                const selectors = ["#floorplan-tools", ".floorplan-tool-options", ".floorplan-canvas-controls", ".floorplan-canvas-floors", ".map-bottom-controls", ".leaflet-control-zoom"];
                const boxes = selectors
                    .map((selector) => ({ selector, node: document.querySelector(selector) }))
                    .filter((entry) => entry.node && !(entry.node as HTMLElement).hidden)
                    .map((entry) => ({ selector: entry.selector, rect: (entry.node as HTMLElement).getBoundingClientRect() }))
                    .filter((entry) => entry.rect.width > 0 && entry.rect.height > 0);
                const clashes: string[] = [];
                for (let i = 0; i < boxes.length; i++) {
                    for (let j = i + 1; j < boxes.length; j++) {
                        const a = boxes[i]!;
                        const b = boxes[j]!;
                        const gap = a.rect.right <= b.rect.left || b.rect.right <= a.rect.left || a.rect.bottom <= b.rect.top || b.rect.bottom <= a.rect.top;
                        if (!gap) clashes.push(`${a.selector} over ${b.selector}`);
                    }
                }
                return clashes;
            });

            expect(overlaps, `at ${viewport.width}px`).toEqual([]);
            await page.close();
        }
    });

    test("the plan can be reached and moved with the keyboard alone", async () => {
        // The canvas had no keyboard path to anything: geometry could be drawn,
        // selected, moved and deleted only with a pointer.
        await openEditor();
        await page.locator("#floorplan-map").focus();

        await page.keyboard.press("Tab");
        expect(await page.locator("#floorplan-form h3").count()).toBe(1);
        expect(await page.locator("#floorplan-live").textContent()).toContain("wall");

        const before = await planExtent();
        for (let press = 0; press < 12; press++) await page.keyboard.press("Shift+ArrowRight");
        await settle();

        // One wall moved east, so the plan reaches further east than it did.
        const after = await planExtent();
        expect(after.width).toBeGreaterThan(before.width + 10);
        await page.close();
    });

    test("Shift+Tab steps back, and Escape gives focus up rather than trapping it", async () => {
        await openEditor();
        await page.locator("#floorplan-map").focus();
        await page.keyboard.press("Tab");
        const first = await page.locator("#floorplan-live").textContent();
        await page.keyboard.press("Tab");
        expect(await page.locator("#floorplan-live").textContent()).not.toBe(first);

        await page.keyboard.press("Shift+Tab");
        expect(await page.locator("#floorplan-live").textContent()).toBe(first);

        // Once for the selection, once to leave. Taking Tab is only defensible
        // because this gives it back.
        await page.keyboard.press("Escape");
        await page.keyboard.press("Escape");
        expect(await page.evaluate(() => document.activeElement?.id)).not.toBe("floorplan-map");
        await page.close();
    });

    test("box select follows the rectangle on screen, even when the plan is turned", async () => {
        // The selection used to be tested against a latitude/longitude box built
        // from the rectangle's corners, which is only the same shape while the
        // map faces north. Turning the plan to face its building is the first
        // thing anyone does here.
        await openEditor();
        await page.locator('[data-tool="box"]').click();

        const selectAll = async (): Promise<number> => {
            const frame = await page.locator("#floorplan-map").boundingBox();
            if (!frame) throw new Error("no map");
            await page.mouse.move(frame.x + 4, frame.y + 4);
            await page.mouse.down();
            await page.mouse.move(frame.x + frame.width - 4, frame.y + frame.height - 4, { steps: 8 });
            await page.mouse.up();
            await settle();
            const heading = await page.locator("#floorplan-form h3").first().textContent();
            const match = /^(\d+) items/.exec(heading || "");
            return match ? Number(match[1]) : 0;
        };

        const facingNorth = await selectAll();
        expect(facingNorth).toBeGreaterThanOrEqual(4);

        // Turn the plan with the tool that does it, rather than reaching into
        // the map from the test - the rotation path is worth exercising too.
        await page.keyboard.press("Escape");
        await page.locator('[data-tool="rotate"]').click();
        const frame = await page.locator("#floorplan-map").boundingBox();
        if (!frame) throw new Error("no map");
        const centre = { x: frame.x + frame.width / 2, y: frame.y + frame.height / 2 };
        await page.mouse.move(centre.x + 160, centre.y);
        await page.mouse.down();
        await page.mouse.move(centre.x + 130, centre.y + 90, { steps: 10 });
        await page.mouse.up();
        await settle();

        await page.keyboard.press("Escape");
        await page.locator('[data-tool="box"]').click();
        const turned = await selectAll();

        expect(turned).toBe(facingNorth);
        await page.close();
    });

    test("drawing a wall with a finger shows the rubber band", async () => {
        // The preview was driven by Leaflet's mousemove, which a finger never
        // emits, so on a phone every corner was placed blind: no rubber band,
        // no length, no snap readout.
        await openEditor();
        await page.locator('[data-tool="wall"]').click();
        const frame = await page.locator("#floorplan-map").boundingBox();
        if (!frame) throw new Error("no map");
        const start = { x: frame.x + frame.width / 2, y: frame.y + frame.height / 2 };

        // Place the first corner, then aim for the second with a finger down.
        await page.mouse.click(start.x, start.y);
        await settle();
        // Captured after the mouse has already drawn one: the question is not
        // whether a rubber band exists, it is whether it follows the finger.
        const ghostPath = () => page.evaluate(() => document.querySelector('#floorplan-map path[stroke-dasharray="5 5"]')?.getAttribute("d") ?? "");
        const beforeTouch = await ghostPath();

        await page.evaluate((from) => {
            const at = from as { x: number; y: number };
            const map = document.getElementById("floorplan-map") as HTMLElement;
            const fire = (type: string, x: number, y: number): void => {
                map.dispatchEvent(new PointerEvent(type, { pointerId: 9, pointerType: "touch", isPrimary: true, bubbles: true, cancelable: true, clientX: x, clientY: y, buttons: type === "pointerup" ? 0 : 1 }));
            };
            fire("pointerdown", at.x + 20, at.y);
            for (let step = 1; step <= 6; step++) fire("pointermove", at.x + 20 + step * 10, at.y);
        }, start);
        await settle();

        const afterTouch = await ghostPath();
        expect(beforeTouch).not.toBe("");
        expect(afterTouch).not.toBe(beforeTouch);
        await page.close();
    });

    test("a slide-and-lift finger gesture actually places the corner", async () => {
        // Driven through CDP rather than synthetic DOM events, because the
        // question is exactly what the browser's own touch pipeline does with a
        // finger that moves before it lifts. A hand-dispatched MouseEvent would
        // be assuming the answer.
        //
        // Desktop viewport deliberately: this asks about touch semantics, not
        // layout, and the harness pins the map to 900px (see HARNESS), so at
        // phone width its own CSS - not the site's - decides what is where.
        await openEditor({ width: 1200, height: 800 }, true);
        const before = await page.locator(".floorplan-wall").count();
        await page.locator('[data-tool="wall"]').click();
        const frame = await page.locator("#floorplan-map").boundingBox();
        if (!frame) throw new Error("no map");
        const at = { x: frame.x + 200, y: frame.y + frame.height - 80 };

        const cdp = await page.context().newCDPSession(page);
        const stroke = async (x0: number, y0: number, x1: number, y1: number): Promise<void> => {
            await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: x0, y: y0 }] });
            for (let step = 1; step <= 5; step++) {
                await cdp.send("Input.dispatchTouchEvent", {
                    type: "touchMove",
                    touchPoints: [{ x: x0 + ((x1 - x0) * step) / 5, y: y0 + ((y1 - y0) * step) / 5 }],
                });
            }
            await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
        };
        await stroke(at.x, at.y, at.x + 30, at.y);
        await stroke(at.x + 30, at.y, at.x + 160, at.y);
        // Tapping the last corner again finishes the chain open-ended.
        await stroke(at.x + 160, at.y, at.x + 160, at.y);
        await settle();

        expect(await page.locator(".floorplan-wall").count()).toBe(before + 1);
        await page.close();
    });

    test("tapping the zoom control does not drop a corner underneath it", async () => {
        // Leaflet's controls live inside the map container and stop their own
        // click from reaching the map - but they stop "click", not
        // "pointerdown", so a handler listening for pointers sees every tap on
        // every control. The mouse path is protected by Leaflet; the touch one
        // has to exclude them itself, which is what the two drag handlers
        // above already do.
        await openEditor({ width: 1200, height: 800 }, true);
        await page.locator('[data-tool="wall"]').click();
        const dashed = () => page.evaluate(() => document.querySelectorAll('#floorplan-map path[stroke-dasharray="5 5"]').length);
        expect(await dashed()).toBe(0);

        const zoomIn = await page.locator(".leaflet-control-zoom-in").boundingBox();
        if (!zoomIn) throw new Error("no zoom control");
        const at = { x: zoomIn.x + zoomIn.width / 2, y: zoomIn.y + zoomIn.height / 2 };
        const cdp = await page.context().newCDPSession(page);
        await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: at.x, y: at.y }] });
        await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
        await settle();

        // No rubber band means no corner was placed.
        expect(await dashed()).toBe(0);
        await page.close();
    });

    test("a tap that dismisses a popup does not also draw", async () => {
        // The click path has always known this: a click whose only job was to
        // close an open popup should not also act on the map. The flag it
        // consults was set on mousedown, which a finger never fires.
        await openEditor({ width: 1200, height: 800 }, true);
        await page.locator('[data-tool="marker"]').click();
        const frame = await page.locator("#floorplan-map").boundingBox();
        if (!frame) throw new Error("no map");
        await page.mouse.click(frame.x + 260, frame.y + frame.height - 90);
        await settle();
        // Placing selects it; clicking it in select mode is what opens the popup.
        await page.locator('[data-tool="select"]').click();
        await page.locator(".leaflet-marker-icon").first().click();
        await settle();
        // Waited for, not asserted outright: the marker layer is rebuilt on
        // select, so for a moment the outgoing popup is still fading out
        // alongside the incoming one.
        await page.waitForFunction(() => document.querySelectorAll(".leaflet-popup").length === 1, undefined, { timeout: 5000 });

        await page.locator('[data-tool="wall"]').click();
        const dashed = () => page.evaluate(() => document.querySelectorAll('#floorplan-map path[stroke-dasharray="5 5"]').length);
        expect(await dashed()).toBe(0);

        const at = { x: frame.x + 480, y: frame.y + frame.height - 90 };
        const cdp = await page.context().newCDPSession(page);
        await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: at.x, y: at.y }] });
        await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
        await settle();

        expect(await dashed()).toBe(0);
        await page.close();
    });

    test("the floor strip adds where it says it will, basements included", async () => {
        // The strip is the building seen from the side. There used to be one
        // "+ Floor" button, at the bottom, which added a floor at the top - and
        // no way to make a basement at all, which is half of what a plan of a
        // derelict building needs.
        await openEditor();
        const strip = "#floorplan-floors";
        const chips = () => page.evaluate((sel) => Array.from(document.querySelectorAll(`${sel} .floorplan-floor-tab__chip`)).map((n) => (n.textContent ?? "").trim()), strip);
        expect(await chips()).toEqual(["G"]);

        await page.locator(`${strip} [aria-label="Add floor above"]`).click();
        await settle();
        // Highest first: the strip reads top-of-building downwards.
        expect(await chips()).toEqual(["1", "G"]);

        await page.locator(`${strip} [aria-label="Add floor below"]`).click();
        await settle();
        expect(await chips()).toEqual(["1", "G", "B1"]);

        // And each button sits at the end of the strip it acts on.
        const order = await page.evaluate((sel) => {
            const host = document.querySelector(sel as string) as HTMLElement;
            const y = (node: Element | null) => (node ? node.getBoundingClientRect().top : Number.NaN);
            return {
                above: y(host.querySelector('[aria-label="Add floor above"]')),
                top: y(host.querySelector(".floorplan-floor-tab")),
                bottom: y(host.querySelectorAll(".floorplan-floor-tab")[2] ?? null),
                below: y(host.querySelector('[aria-label="Add floor below"]')),
            };
        }, strip);
        expect(order.above).toBeLessThan(order.top);
        expect(order.below).toBeGreaterThan(order.bottom);
        await page.close();
    });

    test("a marker's colour is picked with the site's own colour picker", async () => {
        // Not a set of swatches this editor grew for itself: the same markup,
        // classes and pickColor() call as the label and pin dialogs, so the
        // control cannot drift from the rest of the site. That means the page
        // has to install the window global those inline handlers call - which
        // only two other entries did.
        await openEditor();
        await page.locator('[data-tool="marker"]').click();
        const frame = await page.locator("#floorplan-map").boundingBox();
        if (!frame) throw new Error("no map");
        await page.mouse.click(frame.x + 300, frame.y + frame.height - 100);
        await settle();

        const panel = page.locator("#floorplan-marker-appearance");
        expect(await page.evaluate(() => (document.getElementById("floorplan-marker-appearance") as HTMLElement).hidden)).toBe(false);
        await panel.locator('.color-swatch[data-color="#e53935"]').click();
        await settle();

        // The swatch shows as picked, and the marker actually took the colour.
        expect(await panel.locator(".color-swatch.selected").getAttribute("data-color")).toBe("#e53935");
        const saved = await page.evaluate(() => (document.getElementById("color-value-floorplan-marker") as HTMLInputElement).value);
        expect(saved).toBe("#e53935");
        expect(await page.locator('#floorplan-map .leaflet-marker-icon [style*="e53935"], #floorplan-map [style*="#e53935"]').count()).toBeGreaterThan(0);
        await page.close();
    });

    test("room labels stand down while a drag is running", async () => {
        // A room label is a permanent Leaflet tooltip - a DOM node Leaflet
        // positions itself - and render() rebuilt every one of them on every
        // frame of every drag. This is the behaviour behind the timing below,
        // asserted directly because a threshold alone would not say what broke.
        await openEditor({ width: 1200, height: 800 }, false, "grid");
        // Exactly one label per face, not "some". The old drag path left a
        // stale generation of tooltips behind and read 288 for 144 rooms, and
        // an at-least assertion cannot tell a duplicate from a redraw.
        //
        // Waited for rather than sampled: 144 tooltips do not all arrive in the
        // same frame on a loaded machine, and a count read too early is a
        // flake, not a finding. A count stuck at the wrong number still fails,
        // which is the whole point of the exact figure.
        const ROOMS = 144;
        const settleLabels = async (want: number): Promise<void> => {
            await page.waitForFunction((n) => document.querySelectorAll(".floorplan-room-label").length === n, want, { timeout: 10000 });
        };
        await settleLabels(ROOMS);

        const before = await planExtent();
        await page.mouse.move(before.grab.x, before.grab.y);
        await page.mouse.down();
        for (let step = 1; step <= 4; step++) await page.mouse.move(before.grab.x, before.grab.y - step * 6);
        await settleLabels(0);

        await page.mouse.up();
        await settle();
        // And come back on release, which needs a frame after the drag ends.
        // Back, and not doubled: a render that left the previous frame's
        // tooltips behind would show here as 288.
        await settleLabels(ROOMS);
        await page.close();
    });

    test("a survey-sized plan still drags at a usable frame rate", async () => {
        // The same gesture is run on the four-wall plan first and subtracted.
        // Playwright dispatches each move over a pipe, which costs about as
        // much as a small redraw; timing one plan alone measures that pipe as
        // much as the editor. The difference is the editor.
        //
        // The bound is deliberately loose. This runs on whatever machine is
        // free, so a tight one would be flaky - but the regression it exists to
        // catch (rebuilding every room label per frame) cost 205ms, so there is
        // still a wide margin between "slower than we would like" and "broken".
        const MOVES = 10;
        const dragTime = async (plan: "square" | "grid"): Promise<number> => {
            await openEditor({ width: 1200, height: 800 }, false, plan);
            const before = await planExtent();
            await page.mouse.move(before.grab.x, before.grab.y);
            await page.mouse.down();
            const started = Date.now();
            for (let step = 1; step <= MOVES; step++) await page.mouse.move(before.grab.x, before.grab.y - step * 6);
            const elapsed = Date.now() - started;
            await page.mouse.up();
            // A run where nothing moved would be fast and meaningless.
            const after = await planExtent();
            expect(before.top - after.top, `${plan} plan did not move`).toBeGreaterThan(30);
            await page.close();
            return elapsed / MOVES;
        };

        // Best of two, per plan. A single pass picks up whatever else the
        // machine was doing, and a timing test that fails for that reason gets
        // ignored, which is worse than not having one.
        const best = async (plan: "square" | "grid"): Promise<number> => Math.min(await dragTime(plan), await dragTime(plan));
        const small = await best("square");
        const large = await best("grid");
        console.log(`drag: 4 walls ${small.toFixed(1)}ms/move, 312 walls ${large.toFixed(1)}ms/move`);

        expect(large - small).toBeLessThan(80);
    });

    test("holding the snap-off key suspends snapping, and letting go restores it", async () => {
        // The wall tool's readout names the snap it found ("1.20 m - corner"),
        // so what the tool would do is legible without drawing anything.
        await openEditor();
        const wall = await planExtent();
        await page.locator('[data-tool="wall"]').click();
        const readout = async (x: number, y: number): Promise<string> => {
            await page.mouse.move(x, y);
            await settle();
            return page.evaluate(() => document.querySelector(".floorplan-measure")?.textContent ?? "");
        };

        // Start a chain, then aim at an existing corner, which is snappable.
        await page.mouse.click(wall.left + 40, wall.top + 40);
        const snapped = await readout(wall.left, wall.top);
        expect(snapped).toContain("·");

        await page.keyboard.down("`");
        const free = await readout(wall.left + 1, wall.top + 1);
        expect(free).not.toContain("·");

        await page.keyboard.up("`");
        const again = await readout(wall.left, wall.top);
        expect(again).toContain("·");
        await page.close();
    });

    test("the snap-off key does not stick when the window loses focus", async () => {
        // A held key whose keyup lands on somebody else - alt-tab, a system
        // shortcut, the browser's own find bar - leaves the mode latched, and
        // the editor goes on silently not snapping until the key is pressed and
        // released again. The user's report of that is "snapping stopped
        // working", with nothing to reproduce it from.
        await openEditor();
        const wall = await planExtent();
        await page.locator('[data-tool="wall"]').click();
        await page.mouse.click(wall.left + 40, wall.top + 40);

        await page.keyboard.down("`");
        await page.mouse.move(wall.left, wall.top);
        await settle();
        // Focus goes elsewhere while the key is still down, so no keyup ever
        // arrives for it.
        await page.evaluate(() => window.dispatchEvent(new FocusEvent("blur")));
        await page.mouse.move(wall.left + 1, wall.top + 1);
        await page.mouse.move(wall.left, wall.top);
        await settle();

        const readout = await page.evaluate(() => document.querySelector(".floorplan-measure")?.textContent ?? "");
        expect(readout).toContain("·");
        await page.close();
    });

    test("a door can be dragged from one wall onto another", async () => {
        await openEditor();
        const plan = await planExtent();

        // Cut a door into the top wall, then drag it down onto the left one.
        await page.locator('[data-tool="opening"]').click();
        await page.mouse.click(plan.left + plan.width / 2, plan.top);
        await settle();
        const doorMidpoint = async (): Promise<{ x: number; y: number } | null> =>
            page.evaluate(() => {
                const node = document.querySelector("#floorplan-map path.floorplan-opening");
                if (!node) return null;
                const rect = node.getBoundingClientRect();
                return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
            });
        const before = await doorMidpoint();
        expect(before, "no opening was cut").not.toBeNull();

        await page.locator('[data-tool="select"]').click();
        await page.mouse.move(before!.x, before!.y);
        await page.mouse.down();
        for (let step = 1; step <= 12; step++) {
            await page.mouse.move(before!.x - ((before!.x - plan.left) * step) / 12, before!.y + ((plan.height / 2) * step) / 12);
        }
        await page.mouse.up();
        await settle();

        const after = await doorMidpoint();
        expect(after, "the opening vanished").not.toBeNull();
        // It left the top wall: it is now down the left-hand side.
        expect(after!.y - before!.y).toBeGreaterThan(50);
        expect(Math.abs(after!.x - plan.left)).toBeLessThan(30);
        await page.close();
    });

    test("a door's swing is offered where it applies, and drawn once set", async () => {
        // The field was modelled, persisted and round-tripped, and no control
        // ever set it and nothing ever drew it.
        await openEditor();
        const plan = await planExtent();
        await page.locator('[data-tool="opening"]').click();
        await page.mouse.click(plan.left + plan.width / 2, plan.top);
        await settle();
        // Cutting an opening selects it, so the form is already showing it.
        await page.locator('[data-tool="select"]').click();
        await settle();

        const swing = page.locator("#floorplan-form select").nth(1);
        expect(await swing.count()).toBe(1);
        expect(await page.locator("#floorplan-map path.floorplan-door-swing").count()).toBe(0);

        await swing.selectOption("left");
        await settle();
        expect(await page.locator("#floorplan-map path.floorplan-door-swing").count()).toBe(1);

        await swing.selectOption("double");
        await settle();
        expect(await page.locator("#floorplan-map path.floorplan-door-swing").count()).toBe(2);

        // A window has nothing that sweeps, so the question goes away with it.
        await page.locator("#floorplan-form select").first().selectOption("window");
        await settle();
        expect(await page.locator("#floorplan-form select").count()).toBe(1);
        expect(await page.locator("#floorplan-map path.floorplan-door-swing").count()).toBe(0);
        await page.close();
    });

    test("a room inside a building can be moved, and leaves the shell alone", async () => {
        // It could not. A wall counted as the room's own only if it bordered no
        // other face, and in a planar subdivision every partition borders two -
        // so a closet inside a building owned nothing, and the drag, the turn
        // and the delete all declined on the grounds that there was nothing to
        // act on.
        await openEditor({ width: 1200, height: 800 }, false, "closet");
        const shell = async (): Promise<string> =>
            page.evaluate(() => {
                const walls = Array.from(document.querySelectorAll("#floorplan-map path.floorplan-wall"));
                // The building's outline is whatever the walls span overall.
                const boxes = walls.map((n) => n.getBoundingClientRect());
                const left = Math.min(...boxes.map((b) => b.left));
                const top = Math.min(...boxes.map((b) => b.top));
                const right = Math.max(...boxes.map((b) => b.right));
                const bottom = Math.max(...boxes.map((b) => b.bottom));
                return [left, top, right, bottom].map(Math.round).join(",");
            });
        const before = await shell();

        // Select the closet, then drag it.
        const closet = async (): Promise<{ x: number; y: number; w: number } | null> =>
            page.evaluate(() => {
                const fills = Array.from(document.querySelectorAll("#floorplan-map path.floorplan-room"));
                const smallest = fills.map((n) => n.getBoundingClientRect()).sort((a, b) => a.width * a.height - b.width * b.height)[0];
                return smallest ? { x: smallest.left + smallest.width / 2, y: smallest.top + smallest.height / 2, w: Math.round(smallest.width) } : null;
            });
        const room = await closet();
        expect(room, "no room fill").not.toBeNull();
        await page.mouse.click(room!.x, room!.y);
        await settle();

        await page.mouse.move(room!.x, room!.y);
        await page.mouse.down();
        for (let step = 1; step <= 8; step++) await page.mouse.move(room!.x + step * 5, room!.y + step * 2);
        await page.mouse.up();
        await settle();

        // The closet actually went somewhere - without this the test passes
        // just as well when the drag is declined, which is what it is here to
        // catch.
        const moved = await closet();
        expect(moved, "the closet vanished").not.toBeNull();
        expect(Math.abs(moved!.x - room!.x) + Math.abs(moved!.y - room!.y), "the closet did not move").toBeGreaterThan(10);

        // ...and the building is exactly where it was. Both halves are needed:
        // with the old rule the drag was declined, Leaflet took the gesture as
        // a pan, and the closet "moved" only because the whole plan slid under
        // it - which is what this reads as when it is checked on its own.
        expect(await shell(), "the whole plan moved, so that was a pan").toBe(before);
        await page.close();
    });

    test("a free-standing room moves rigidly and can snap to the shell", async () => {
        // Two things at once, because they are the same drag: a room touching
        // nothing translates as a block, and the shell is a snap target for it.
        // The shell used to be excluded on the grounds that this drag stretched
        // it, which it no longer does - it stays exactly where it is, which is
        // what makes it worth lining up against.
        await openEditor({ width: 1200, height: 800 }, false, "island");
        const island = async (): Promise<{ x: number; y: number; w: number; h: number } | null> =>
            page.evaluate(() => {
                const fills = Array.from(document.querySelectorAll("#floorplan-map path.floorplan-room"));
                const smallest = fills.map((n) => n.getBoundingClientRect()).sort((a, b) => a.width * a.height - b.width * b.height)[0];
                return smallest ? { x: Math.round(smallest.left), y: Math.round(smallest.top), w: Math.round(smallest.width), h: Math.round(smallest.height) } : null;
            });
        const before = await island();
        expect(before, "no island").not.toBeNull();

        const grab = { x: before!.x + before!.w / 2, y: before!.y + before!.h / 2 };
        await page.mouse.click(grab.x, grab.y);
        await settle();
        await page.mouse.move(grab.x, grab.y);
        await page.mouse.down();
        for (let step = 1; step <= 10; step++) await page.mouse.move(grab.x - step * 6, grab.y);
        await page.mouse.up();
        await settle();

        const after = await island();
        expect(after, "the island vanished").not.toBeNull();
        // It went somewhere...
        expect(before!.x - after!.x).toBeGreaterThan(20);
        // ...as a block: a room that deformed would not keep its size. Within a
        // pixel, since the box is measured off a rendered path.
        expect(Math.abs(after!.w - before!.w)).toBeLessThanOrEqual(1);
        expect(Math.abs(after!.h - before!.h)).toBeLessThanOrEqual(1);
        await page.close();
    });

    test("undo takes back a typed name in one press, and the drag before it in the next", async () => {
        // "Undo sometimes undoes one thing, sometimes a group" is the report.
        // Both are right, and the rule is what has to be predictable: a run of
        // keystrokes in one field is one step, and anything else is its own.
        await openEditor({ width: 1200, height: 800 }, false, "island");
        // Waited for, not read: renaming rebuilds the room layer, and Leaflet
        // fades a tooltip out over 200ms rather than removing it, so for that
        // long the outgoing label is still in the pane beside the incoming one
        // and whichever is read first is a coin toss. Nobody sees two labels -
        // the outgoing one is at opacity 0 throughout - but a test reading the
        // DOM does.
        const expectRoomNamed = async (want: string): Promise<void> => {
            await page.waitForFunction(
                (expected) => {
                    const named = Array.from(document.querySelectorAll(".floorplan-room-label__name"))
                        .map((n) => (n.textContent ?? "").trim())
                        .filter((text) => text !== "Unnamed room");
                    return named.length === 1 && named[0] === expected;
                },
                want,
                { timeout: 10000 },
            );
        };
        const island = await page.evaluate(() => {
            const fills = Array.from(document.querySelectorAll("#floorplan-map path.floorplan-room"));
            const smallest = fills.map((n) => n.getBoundingClientRect()).sort((a, b) => a.width * a.height - b.width * b.height)[0];
            return smallest ? { x: smallest.left + smallest.width / 2, y: smallest.top + smallest.height / 2 } : null;
        });
        expect(island).not.toBeNull();
        await expectRoomNamed("Island");

        // One drag...
        await page.mouse.click(island!.x, island!.y);
        await settle();
        await page.mouse.move(island!.x, island!.y);
        await page.mouse.down();
        for (let step = 1; step <= 8; step++) await page.mouse.move(island!.x - step * 5, island!.y);
        await page.mouse.up();
        await settle();
        const moved = await page.evaluate(() => {
            const fills = Array.from(document.querySelectorAll("#floorplan-map path.floorplan-room"));
            const smallest = fills.map((n) => n.getBoundingClientRect()).sort((a, b) => a.width * a.height - b.width * b.height)[0];
            return smallest ? Math.round(smallest.left) : -1;
        });

        // ...then a name, typed a character at a time.
        const field = page.locator("#floorplan-form input.form-input").first();
        await field.click();
        await field.fill("");
        await page.keyboard.type("Plant room", { delay: 15 });
        await field.blur();
        await settle();
        await expectRoomNamed("Plant room");

        // One press takes back the whole name, not one letter of it.
        await page.locator("#floorplan-undo").click();
        await settle();
        await expectRoomNamed("Island");

        // The next takes back the drag, which was never part of that group.
        await page.locator("#floorplan-undo").click();
        await settle();
        const back = await page.evaluate(() => {
            const fills = Array.from(document.querySelectorAll("#floorplan-map path.floorplan-room"));
            const smallest = fills.map((n) => n.getBoundingClientRect()).sort((a, b) => a.width * a.height - b.width * b.height)[0];
            return smallest ? Math.round(smallest.left) : -1;
        });
        expect(back).toBeGreaterThan(moved);
        await page.close();
    });

    test("duplicating a floor copies the plan and leaves the original alone", async () => {
        // Re-tracing an exterior by hand for every storey is the bulk of the
        // work in a multi-floor plan, and the copy must not carry the source's
        // uuids: the server matches items to rows by uuid and deletes by
        // omission, so a carried-over one would move the source floor's rows
        // rather than duplicate them - emptying the floor that was copied from.
        await openEditor({ width: 1200, height: 800 }, false, "closet");
        const chips = () => page.evaluate(() => Array.from(document.querySelectorAll("#floorplan-floors .floorplan-floor-tab__chip")).map((n) => (n.textContent ?? "").trim()));
        const wallCount = () => page.locator(".floorplan-wall").count();
        expect(await chips()).toEqual(["G"]);
        const before = await wallCount();
        expect(before).toBeGreaterThan(4);

        await page.locator('#floorplan-floors [aria-label="Duplicate this floor"]').click();
        await settle();

        // A new storey above, and it is the one being shown - without checking
        // which tab is active, an empty copy would pass on the source's walls.
        expect(await chips()).toEqual(["1", "G"]);
        const active = () => page.evaluate(() => document.querySelector("#floorplan-floors .btn--primary .floorplan-floor-tab__chip")?.textContent?.trim() ?? "");
        expect(await active()).toBe("1");
        expect(await wallCount()).toBe(before);

        // The floor it was copied from still has everything it had.
        await page.locator('#floorplan-floors button:has(.floorplan-floor-tab__chip:text-is("G"))').click();
        await settle();
        expect(await wallCount()).toBe(before);
        await page.close();
    });

    test("a duplicate lands above the floor it was copied from, not on top of the building", async () => {
        // On a one-storey plan those are the same place, which is why this went
        // unnoticed: duplicating the ground floor of a three-storey building
        // put the copy on the roof.
        await openEditor({ width: 1200, height: 800 }, false, "closet");
        const strip = () =>
            page.evaluate(() =>
                Array.from(document.querySelectorAll("#floorplan-floors .floorplan-floor-tab")).map((tab) => [
                    (tab.querySelector(".floorplan-floor-tab__chip")?.textContent ?? "").trim(),
                    (tab.querySelector(".floorplan-floor-tab__name")?.textContent ?? "").trim(),
                ]),
            );
        // Two empty storeys on top of the named ground floor.
        await page.locator('#floorplan-floors [aria-label="Add floor above"]').click();
        await settle();
        await page.locator('#floorplan-floors [aria-label="Add floor above"]').click();
        await settle();
        expect(await strip()).toEqual([
            ["2", ""],
            ["1", ""],
            ["G", "Ground"],
        ]);

        await page.locator('#floorplan-floors button:has(.floorplan-floor-tab__chip:text-is("G"))').click();
        await settle();
        await page.locator('#floorplan-floors [aria-label="Duplicate this floor"]').click();
        await settle();

        // The copy carries the source's nickname, so it is the one sitting
        // directly above it - everything else having been pushed up a storey.
        expect(await strip()).toEqual([
            ["3", ""],
            ["2", ""],
            ["1", "Ground"],
            ["G", "Ground"],
        ]);
        await page.close();
    });

    test("a room whose enclosure is gone takes its name with it", async () => {
        // Otherwise the seed stays behind as a dot labelled "Room - not
        // enclosed", sitting in the middle of nothing: the name belonged to a
        // region that no longer exists.
        //
        // Deleting one wall of a room *inside* a building is a different case
        // and deliberately behaves differently - the region merges with its
        // surroundings and the name goes with it, because the seed still lands
        // in a face. This is the case where it lands in none.
        await openEditor();
        const centre = await planCentre();
        await page.mouse.click(centre.x, centre.y, { button: "right" });
        await page.waitForSelector("text=Name this space", { timeout: 10000 });
        await page.locator("text=Name this space").click();
        await settle();
        const field = page.locator("#floorplan-form input.form-input").first();
        await field.fill("The shed");
        await field.blur();
        await settle();
        await page.waitForFunction(
            () => Array.from(document.querySelectorAll(".floorplan-room-label__name")).some((n) => (n.textContent ?? "").trim() === "The shed"),
            undefined,
            { timeout: 10000 },
        );

        // One wall of the only enclosure, so nothing encloses anything after it.
        const plan = await planExtent();
        await page.mouse.click(plan.grab.x, plan.grab.y);
        await settle();
        const walls = await page.locator(".floorplan-wall").count();
        await page.keyboard.press("Delete");
        await settle();
        expect(await page.locator(".floorplan-wall").count()).toBeLessThan(walls);

        await page.waitForFunction(
            () => !Array.from(document.querySelectorAll(".floorplan-room-label__name")).some((n) => (n.textContent ?? "").trim() === "The shed"),
            undefined,
            { timeout: 10000 },
        );
        // And no dot where the room used to be.
        expect(await page.evaluate(() => document.querySelectorAll('#floorplan-map path[stroke="#ef6c00"]').length)).toBe(0);
        await page.close();
    });

    test("a plan saved elsewhere stops autosaving and says so", async () => {
        // The one failure that must never be retried into: the other tab is not
        // going to un-save, so every further attempt either fails the same way
        // or overwrites their work.
        saves.conflict = true;
        saves.attempts = 0;
        try {
            await openEditor();
            const plan = await planExtent();

            // An edit, which schedules an autosave.
            await page.mouse.click(plan.grab.x, plan.grab.y);
            await settle();
            await page.keyboard.press("ArrowRight");
            await page.waitForFunction(() => document.getElementById("floorplan-save-status")?.textContent === "Changed elsewhere", undefined, { timeout: 15000 });

            const after = saves.attempts;
            expect(after).toBeGreaterThan(0);

            // Reload is offered: told to reload and given nothing to press is a
            // dead end.
            expect(await page.evaluate(() => (document.getElementById("floorplan-reload") as HTMLElement).hidden)).toBe(false);
            expect(await page.evaluate(() => (document.getElementById("floorplan-retry-save") as HTMLElement).hidden)).toBe(true);

            // And further edits do not go on hammering it.
            for (let nudge = 0; nudge < 3; nudge++) {
                await page.keyboard.press("ArrowRight");
                await settle();
            }
            // Comfortably past queueAutosave's 1200ms debounce: at 1500 a timer
            // running late would land after the assertion and the test would
            // pass for the wrong reason.
            await page.waitForTimeout(2500);
            expect(saves.attempts, "kept trying after the conflict").toBe(after);
            await page.close();
        } finally {
            saves.conflict = false;
        }
    });

    test("a door's locks can be recorded, and a window is not asked", async () => {
        // "Is this door locked" is close to the most useful thing a plan of a
        // derelict building can say, and it was modelled, stored and served
        // without ever being reachable from the editor.
        await openEditor();
        const plan = await planExtent();
        await page.locator('[data-tool="opening"]').click();
        await page.mouse.click(plan.left + plan.width / 2, plan.top);
        await settle();
        await page.locator('[data-tool="select"]').click();
        await settle();

        const locks = () => page.locator(".floorplan-lock").count();
        expect(await locks()).toBe(0);
        await page.locator("#floorplan-form .floorplan-locks button", { hasText: "Lock" }).click();
        await settle();
        expect(await locks()).toBe(1);

        // A door can carry several: a padlock and a chain are two answers.
        const row = page.locator(".floorplan-lock").first();
        await row.locator("input.form-input").fill("padlock");
        await row.locator("select").selectOption("locked");
        await settle();
        await page.locator("#floorplan-form .floorplan-locks button", { hasText: "Another lock" }).click();
        await settle();
        expect(await locks()).toBe(2);

        // The first one kept what it was given.
        expect(await page.locator(".floorplan-lock").first().locator("input.form-input").inputValue()).toBe("padlock");
        expect(await page.locator(".floorplan-lock").first().locator("select").inputValue()).toBe("locked");

        // A lock is a floorplan item like any other, so it carries the same
        // details block - which is where "broken" belongs, as opposed to the
        // state above, which asks only whether the door is presently secured.
        const details = page.locator(".floorplan-lock-entry").first().locator("details.floorplan-details");
        expect(await details.count()).toBe(1);
        await details.locator("summary").click();
        // Material, then Condition - the same block every other item gets.
        const condition = details.locator("input.form-input").nth(1);
        await condition.fill("seized, rusted shut");
        await condition.blur();
        await settle();

        // Removing the other one rebuilds the whole panel, so a value that
        // survives that was written to the lock and not just to the input.
        await page.locator(".floorplan-lock").nth(1).locator("button").click();
        await settle();
        expect(await locks()).toBe(1);
        const survived = page.locator(".floorplan-lock-entry").first().locator("details input.form-input").nth(1);
        expect(await survived.inputValue()).toBe("seized, rusted shut");
        expect(await page.locator(".floorplan-lock").first().locator("input.form-input").inputValue()).toBe("padlock");

        // A window has no lock worth recording for getting in, so it is not
        // asked - the question goes away with the type.
        await page.locator("#floorplan-form select").first().selectOption("window");
        await settle();
        expect(await page.locator(".floorplan-locks").count()).toBe(0);
        await page.close();
    });

    test("the measurements the model stores can actually be entered", async () => {
        // built_date, sill_meters, height_meters and elevation_meters are all
        // declared client-side, sent, parsed and stored, and none of them had
        // any way to be filled in.
        await openEditor();
        const plan = await planExtent();

        // A storey's own dimensions, on the floor panel.
        const ceiling = page.locator('#floorplan-floor-fields input[aria-label="Ceiling height"]');
        await ceiling.fill("2.4");
        await page.locator('#floorplan-floor-fields input[aria-label="Ground level"]').fill("41.5");
        await settle();

        // A window's sill, on the opening panel.
        await page.locator('[data-tool="opening"]').click();
        await page.mouse.click(plan.left + plan.width / 2, plan.top);
        await settle();
        await page.locator('[data-tool="select"]').click();
        await settle();
        const sill = page.locator("#floorplan-form input[type=number]").first();
        await sill.fill("1.2");
        await settle();

        // And a date on the item itself, which is a date input rather than free
        // text: the column is a DateField and the serializer parses it
        // strictly, so "1897" would refuse the whole save rather than be stored
        // as a fuzzy year.
        const details = page.locator("#floorplan-form details.floorplan-details").first();
        await details.locator("summary").click();
        const built = details.locator("input[type=date]");
        expect(await built.count()).toBe(1);
        await built.fill("1897-06-01");
        await settle();

        // Everything survives a rebuild of the panel, so it went to the model.
        await page.keyboard.press("Escape");
        await settle();
        expect(await ceiling.inputValue()).toBe("2.4");
        expect(await page.locator('#floorplan-floor-fields input[aria-label="Ground level"]').inputValue()).toBe("41.5");
        await page.close();
    });

    test("the tool options panel shows the armed tool's choices", async () => {
        await openEditor();
        await page.locator('[data-tool="opening"]').click();

        const panel = page.locator("#floorplan-tool-options");
        expect(await panel.getAttribute("hidden")).toBeNull();
        expect(await panel.textContent()).toContain("Window");
        await page.close();
    });
});
