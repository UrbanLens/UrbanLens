/**
 * Sanborn maps found automatically for the parcel becoming georeferenced, toggleable overlays.
 *
 * Nothing auto-creates a `MapImageOverlay` today (see scratchpad/map/sanborn.json) - every
 * construction site is a manual click. This file drives the rendering and toggle machinery that
 * already exists (`#map-overlays-data`, the "Overlays" dialog, `map-image-overlays.ts`) and treats
 * only the missing creation step as the gap.
 *
 * UI contract for whatever creates the overlay automatically:
 * - Build it the same way `HistoricalMapBrowseView.post` already does (a locked `MapImageOverlay`
 *   with `tile_url_template` pointing at `map.historical_tiles`, `corners` from the georeference's
 *   bounds), so it shows up in `#map-overlays-data` and in the existing manage dialog with no new
 *   markup: `.map-overlay-manage-row[data-overlay-uuid]` + `input[name="default_visible"]` already
 *   round-trip through `ul:map-overlays-changed` to the live map.
 * - `name` must match `/sanborn/i` so a user (and this file) can identify it without opening REData's
 *   landing page.
 */

import type { Locator, Page } from "@playwright/test";

import { ensureCampusWiki, expect, locationDataTest as test, skipUnlessLocationDataEnabled } from "./fixtures.js";
import { containsCoordinate, geometryPositions, HRSH_PIN, hrshRoutes, metresBetween, type Coordinate, type GeoJsonGeometry } from "../../lib/hrsh.js";
import { recordMetric, type MetricTags } from "../../lib/metrics.js";
import { pinDetail } from "../../lib/routes.js";
import { RunScopedStore } from "../../lib/run.js";
import { waitForOrNull } from "../../lib/waiting.js";

skipUnlessLocationDataEnabled();

const SANBORN_NAME_PATTERN = /sanborn/i;
const LOC_ATTRIBUTION_PATTERN = /library of congress/i;

const OVERLAY_WAIT_MS = 300_000;
const OVERLAY_POLL_MS = 15_000;
const GALLERY_WAIT_MS = 150_000;

/** A Sanborn sheet is a city-block-scale map; this is city-scale, not "the whole county". */
const MAX_SHEET_EXTENT_M = 5_000;

/** How close the overlay's bounds must come to the requirement's own pin. */
const MAX_DISTANCE_FROM_PIN_M = 500;

const THIRD_PARTY_TILE_HOSTS = [/wayback\.maptiles\.arcgis\.com/, /tile\.openstreetmap\.org/, /server\.arcgisonline\.com/];

/** One entry of `MapImageOverlay.to_json()`. */
interface OverlayEntry {
    uuid: string;
    name: string;
    url: string;
    tile_url_template: string;
    corners: [number, number][];
    default_visible: boolean;
}

interface OverlayProbe {
    entries: OverlayEntry[];
    sanborn: OverlayEntry | null;
}

/** Reads `#map-overlays-data` off whatever page is currently loaded - no dialog, no click. */
async function readOverlays(page: Page): Promise<OverlayEntry[]> {
    const raw = await page.locator("#map-overlays-data").textContent();
    try {
        const parsed = raw ? JSON.parse(raw) : [];
        return Array.isArray(parsed) ? parsed : [];
    } catch {
        return [];
    }
}

function findSanborn(entries: OverlayEntry[]): OverlayEntry | null {
    return entries.find((entry) => SANBORN_NAME_PATTERN.test(entry.name)) ?? null;
}

const exhaustedWaits = new RunScopedStore<Record<string, string>>("hrsh-sanborn-exhausted-waits");

function isExhausted(key: string): boolean {
    return Boolean(exhaustedWaits.read()?.[key]);
}

function markExhausted(key: string): void {
    exhaustedWaits.write({ ...(exhaustedWaits.read() ?? {}), [key]: new Date().toISOString() });
}

/**
 * Waits for a Sanborn-named overlay to appear via `goto`, polling by reloading. Records
 * `hrsh.sanborn.overlay_count`/`.map_count` every call, and `.seconds_to_present` only when it
 * actually had to wait. A wait that already ran out this run is not repeated.
 */
async function waitForSanbornOverlay(page: Page, goto: () => Promise<void>, key: string, label: string, tags: MetricTags): Promise<OverlayProbe> {
    const startedAt = Date.now();
    await goto();
    let entries = await readOverlays(page);

    if (findSanborn(entries) === null && !isExhausted(key)) {
        const settled = await waitForOrNull(
            async () => {
                await goto();
                entries = await readOverlays(page);
                return entries;
            },
            (list) => findSanborn(list) !== null,
            {
                what: `an auto-created Sanborn-map overlay on ${label}`,
                timeoutMs: OVERLAY_WAIT_MS,
                intervalMs: OVERLAY_POLL_MS,
                describe: (list) => (list.length ? `${list.length} overlay(s): ${list.map((entry) => entry.name || "(unnamed)").join(", ")}` : "no overlays at all"),
            },
        );
        if (settled) {
            entries = settled;
            recordMetric({ name: "hrsh.sanborn.seconds_to_present", value: Math.round((Date.now() - startedAt) / 1000), unit: "s", tags });
        } else {
            markExhausted(key);
        }
    }

    recordMetric({ name: "hrsh.sanborn.overlay_count", value: entries.length, unit: "count", tags });
    recordMetric({ name: "hrsh.sanborn.map_count", value: entries.filter((entry) => SANBORN_NAME_PATTERN.test(entry.name)).length, unit: "count", tags });
    return { entries, sanborn: findSanborn(entries) };
}

/** Skips the calling test when no Sanborn overlay was found; the caller's own gate test reports that as the finding. */
function requireSanborn(probe: OverlayProbe, label: string): asserts probe is { entries: OverlayEntry[]; sanborn: OverlayEntry } {
    test.skip(probe.sanborn === null, `no Sanborn-map overlay exists on ${label} - see the previous test, which reports that as the finding it is.`);
}

interface BoundingBox {
    minLat: number;
    maxLat: number;
    minLon: number;
    maxLon: number;
}

function boundingBoxOf(corners: readonly [number, number][]): BoundingBox {
    const lats = corners.map(([lat]) => lat);
    const lons = corners.map(([, lon]) => lon);
    return { minLat: Math.min(...lats), maxLat: Math.max(...lats), minLon: Math.min(...lons), maxLon: Math.max(...lons) };
}

/** 0 when `point` falls inside `box`; otherwise the distance to its nearest edge. */
function distanceFromBox(point: Coordinate, box: BoundingBox): number {
    const clamp = (value: number, lo: number, hi: number) => Math.min(Math.max(value, lo), hi);
    return metresBetween(point, {
        label: "nearest point on the overlay's bounds",
        latitude: clamp(point.latitude, box.minLat, box.maxLat),
        longitude: clamp(point.longitude, box.minLon, box.maxLon),
    });
}

function boxDiagonalMetres(box: BoundingBox): number {
    return metresBetween({ label: "nw", latitude: box.maxLat, longitude: box.minLon }, { label: "se", latitude: box.minLat, longitude: box.maxLon });
}

/** A cheap overlap approximation: any corner (or the centre) of `box` inside `geometry`, or any vertex of `geometry` inside `box`. */
function boxOverlapsGeometry(box: BoundingBox, geometry: GeoJsonGeometry): boolean {
    const boxPoints: Coordinate[] = [
        { label: "nw", latitude: box.maxLat, longitude: box.minLon },
        { label: "ne", latitude: box.maxLat, longitude: box.maxLon },
        { label: "se", latitude: box.minLat, longitude: box.maxLon },
        { label: "sw", latitude: box.minLat, longitude: box.minLon },
        { label: "centre", latitude: (box.minLat + box.maxLat) / 2, longitude: (box.minLon + box.maxLon) / 2 },
    ];
    if (boxPoints.some((point) => containsCoordinate(geometry, point))) {
        return true;
    }
    return geometryPositions(geometry).some(([lon, lat]) => lat >= box.minLat && lat <= box.maxLat && lon >= box.minLon && lon <= box.maxLon);
}

function tileXY(lon: number, lat: number, zoom: number): { x: number; y: number } {
    const latRad = (lat * Math.PI) / 180;
    const n = 2 ** zoom;
    return { x: Math.floor(((lon + 180) / 360) * n), y: Math.floor(((1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2) * n) };
}

/** City-block zoom: fine enough that a real warped pyramid should have a tile here. */
const TILE_PROBE_ZOOM = 17;

/** The DOM element a rendered overlay shows as, and a URL whose 200 proves it actually serves. */
function renderedOverlay(page: Page, overlay: OverlayEntry): { locator: Locator; probeUrl: string } {
    if (overlay.tile_url_template) {
        const box = boundingBoxOf(overlay.corners);
        const { x, y } = tileXY((box.minLon + box.maxLon) / 2, (box.minLat + box.maxLat) / 2, TILE_PROBE_ZOOM);
        return {
            locator: page.locator(`.leaflet-tile-pane img[src*="/historical-tiles/${overlay.uuid}/"]`),
            probeUrl: overlay.tile_url_template.replace("{z}", String(TILE_PROBE_ZOOM)).replace("{x}", String(x)).replace("{y}", String(y)),
        };
    }
    return { locator: page.locator(`img.ul-map-overlay-image[src="${overlay.url}"]`), probeUrl: overlay.url };
}

/** Opens the map layers menu and the "Overlays" dialog, the one existing user-facing entry point for toggling. */
async function openOverlaysDialog(page: Page): Promise<void> {
    await page.getByRole("button", { name: "Toggle map layers" }).click();
    await page.getByRole("button", { name: "Overlays" }).click();
}

function manageRow(page: Page, uuid: string): Locator {
    return page.locator(`.map-overlay-manage-row[data-overlay-uuid="${uuid}"]`);
}

test.describe("Hudson River State Hospital - Sanborn map overlay (private pin page)", () => {
    test.beforeEach(async ({ guard }) => {
        for (const host of THIRD_PARTY_TILE_HOSTS) {
            guard.allow(host);
        }
    });

    test("a Sanborn-map overlay is present without opening any dialog", async ({ campus, page }) => {
        const probe = await waitForSanbornOverlay(page, async () => void (await page.goto(pinDetail(campus.pin.slug))), "pin", "the private pin page", { surface: "pin" });

        expect(
            probe.sanborn,
            `no overlay in #map-overlays-data has a name matching /sanborn/i (saw: ${probe.entries.map((entry) => entry.name || "(unnamed)").join(", ") || "none"}). ` +
                "Nothing auto-seeds a MapImageOverlay for a Location today - HistoricalMapBrowseView only fires on an explicit dialog " +
                "click (controllers/map_overlays.py). See scratchpad/map/sanborn.json's implementation_sketch for the auto-seed shape.",
        ).not.toBeNull();
    });

    test("it is placed at the parcel, at the scale of a real sheet", async ({ campus, page }) => {
        const probe = await waitForSanbornOverlay(page, async () => void (await page.goto(pinDetail(campus.pin.slug))), "pin", "the private pin page", { surface: "pin" });
        requireSanborn(probe, "the private pin page");

        const box = boundingBoxOf(probe.sanborn.corners);
        const extent = boxDiagonalMetres(box);
        const distance = distanceFromBox(HRSH_PIN, box);

        expect(extent, `the overlay's bounds span ${Math.round(extent).toLocaleString()} m corner-to-corner - too large to be one Sanborn sheet`).toBeLessThan(MAX_SHEET_EXTENT_M);
        expect(
            distance,
            `the overlay's bounds are ${Math.round(distance).toLocaleString()} m from ${HRSH_PIN.latitude}, ${HRSH_PIN.longitude} - too far to be this parcel's sheet`,
        ).toBeLessThan(MAX_DISTANCE_FROM_PIN_M);
    });

    test("it overlaps the resolved parcel boundary", async ({ campus, page }) => {
        campus.requireBoundary();
        const probe = await waitForSanbornOverlay(page, async () => void (await page.goto(pinDetail(campus.pin.slug))), "pin", "the private pin page", { surface: "pin" });
        requireSanborn(probe, "the private pin page");

        const box = boundingBoxOf(probe.sanborn.corners);
        expect(boxOverlapsGeometry(box, campus.boundary!), "the overlay's bounds share no point with the parcel boundary - it is placed somewhere else entirely").toBe(true);
    });

    test("it renders on the map and its own tile/image request answers 200", async ({ campus, page }) => {
        const probe = await waitForSanbornOverlay(page, async () => void (await page.goto(pinDetail(campus.pin.slug))), "pin", "the private pin page", { surface: "pin" });
        requireSanborn(probe, "the private pin page");

        const { locator, probeUrl } = renderedOverlay(page, probe.sanborn);
        await expect(locator.first(), `no rendered element matches the Sanborn overlay "${probe.sanborn.name}" (${probe.sanborn.uuid}) - check map-image-overlays.ts's sync()`).toBeVisible({
            timeout: 30_000,
        });

        const response = await page.request.get(probeUrl);
        expect(response.status(), `the overlay's own request (${probeUrl}) answered ${response.status()}, not 200`).toBe(200);
    });

    test("it can be turned off and back on from the map's layers panel", async ({ campus, page }) => {
        const probe = await waitForSanbornOverlay(page, async () => void (await page.goto(pinDetail(campus.pin.slug))), "pin", "the private pin page", { surface: "pin" });
        requireSanborn(probe, "the private pin page");
        const { uuid } = probe.sanborn;
        const { locator } = renderedOverlay(page, probe.sanborn);
        await expect(locator.first()).toBeVisible({ timeout: 30_000 });

        await openOverlaysDialog(page);
        await expect(manageRow(page, uuid), "the manage-overlays dialog has no row for the auto-created overlay").toBeVisible({ timeout: 15_000 });

        await manageRow(page, uuid).locator('input[name="default_visible"]').uncheck();
        await expect(locator, 'unchecking "Show by default" did not remove the overlay from the map').toHaveCount(0, { timeout: 15_000 });

        // The dialog body is swapped wholesale on every change, so the row is re-queried rather than reused.
        await manageRow(page, uuid).locator('input[name="default_visible"]').check();
        await expect(locator.first(), 're-checking "Show by default" did not restore the overlay').toBeVisible({ timeout: 30_000 });
    });

    test("the automatically found photos for the parcel include a Sanborn map", async ({ campus, page }) => {
        await page.goto(pinDetail(campus.pin.slug));

        await expect
            .poll(
                async () => {
                    // Matched in Node, not inside evaluateAll - a closure over an outer RegExp does not survive
                    // serialization into the page.
                    const attrs = await page.locator(".media-item").evaluateAll((elements) =>
                        elements.map((element) => ({
                            caption: element.getAttribute("data-media-caption") ?? "",
                            source: element.getAttribute("data-media-source-name") ?? "",
                        })),
                    );
                    return attrs.some(({ caption, source }) => SANBORN_NAME_PATTERN.test(caption) || SANBORN_NAME_PATTERN.test(source) || LOC_ATTRIBUTION_PATTERN.test(source));
                },
                {
                    timeout: GALLERY_WAIT_MS,
                    intervals: [5_000],
                    message:
                        "no media tile's caption or source identifies a Sanborn map (checked for /sanborn/i and /library of congress/i). " +
                        "LibraryOfCongressMediaProvider does a generic text search rather than querying REData's georeferenced /maps/ " +
                        "index, so a Sanborn plate may simply never surface here - see scratchpad/map/sanborn.json's gaps",
                },
            )
            .toBe(true);
    });
});

test.describe("Hudson River State Hospital - Sanborn map overlay (wiki page)", () => {
    test.beforeEach(async ({ campus, guard }) => {
        for (const host of THIRD_PARTY_TILE_HOSTS) {
            guard.allow(host);
        }
        const ready = await ensureCampusWiki(campus);
        test.skip(!ready, "no wiki for the campus yet - hrsh-wiki.spec.ts creates it and reports if that fails.");
    });

    test("a Sanborn-map overlay is present without opening any dialog", async ({ campus, page }) => {
        const probe = await waitForSanbornOverlay(page, async () => void (await page.goto(hrshRoutes.wiki(campus.pin.location_slug))), "wiki", "the wiki page", { surface: "wiki" });

        expect(
            probe.sanborn,
            `no overlay in #map-overlays-data has a name matching /sanborn/i (saw: ${probe.entries.map((entry) => entry.name || "(unnamed)").join(", ") || "none"}) on the wiki page`,
        ).not.toBeNull();
    });

    test("it is placed at the parcel, at the scale of a real sheet", async ({ campus, page }) => {
        const probe = await waitForSanbornOverlay(page, async () => void (await page.goto(hrshRoutes.wiki(campus.pin.location_slug))), "wiki", "the wiki page", { surface: "wiki" });
        requireSanborn(probe, "the wiki page");

        const box = boundingBoxOf(probe.sanborn.corners);
        expect(boxDiagonalMetres(box), "the wiki overlay's bounds are too large to be one Sanborn sheet").toBeLessThan(MAX_SHEET_EXTENT_M);
        expect(distanceFromBox(HRSH_PIN, box), "the wiki overlay's bounds are too far from the requirement's pin").toBeLessThan(MAX_DISTANCE_FROM_PIN_M);
    });

    test("it renders on the map", async ({ campus, page }) => {
        const probe = await waitForSanbornOverlay(page, async () => void (await page.goto(hrshRoutes.wiki(campus.pin.location_slug))), "wiki", "the wiki page", { surface: "wiki" });
        requireSanborn(probe, "the wiki page");

        const { locator } = renderedOverlay(page, probe.sanborn);
        await expect(locator.first(), "no rendered element matches the wiki's Sanborn overlay").toBeVisible({ timeout: 30_000 });
    });
});
