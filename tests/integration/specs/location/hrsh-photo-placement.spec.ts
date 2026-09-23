/**
 * Uploading a photo with no GPS on the private pin page, then dragging it onto the map to set its
 * coordinates - map-annotations.ts's photo panel drag/drop onto `#map`, backed by `PinImageView`.
 *
 * UI contract: none needed - every selector below already exists. Relied upon: `#gallery-file-input`
 * (hidden file input, behind the Media card's "Mine" tab, `_photo_gallery.html`), the map sidebar's
 * `#map-panel-tab-photos` / `#photo-panel-list > li.photo-panel-item[data-id]` with a
 * `.photo-panel-coord-badge.no-gps|.has-gps` child, `#map`, and `.photo-marker-img[src]` for a placed
 * photo's marker (`map-annotations.ts`, `shared/photo-map.ts`). If any of these are renamed, update
 * this file alongside.
 */

import { type Locator, type Page, type Response } from "@playwright/test";

import { expect, locationDataTest as test, skipUnlessLocationDataEnabled } from "./fixtures.js";
import { metresBetween, type Coordinate } from "../../lib/hrsh.js";
import { waitForHtmxSettled } from "../../lib/htmx.js";
import { recordMetric } from "../../lib/metrics.js";
import { pinDetail } from "../../lib/routes.js";

skipUnlessLocationDataEnabled();

/** Same origins `hrsh-media.spec.ts` allows: basemap tiles this environment cannot reach, unrelated to this spec's subject. */
const THIRD_PARTY_TILE_HOSTS = [/wayback\.maptiles\.arcgis\.com/, /tile\.openstreetmap\.org/, /server\.arcgisonline\.com/];

/** A minimal valid JPEG with no EXIF segment at all, so it carries no GPS by construction. Same base used by vault-photos.spec.ts. */
const TINY_JPEG_BASE = Buffer.from(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsOCwkJDRENDg8QEBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBD/wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCdABmX/9k=",
    "base64",
);

/** A fresh JPEG per call, so a rerun is never deduped against a leftover upload by checksum. */
function uniqueTinyJpeg(): Buffer {
    return Buffer.concat([TINY_JPEG_BASE, Buffer.from(`${Date.now()}-${Math.random()}`)]);
}

/** How close the server-saved coordinate must land to the pixel the test dropped on. */
const PLACEMENT_TOLERANCE_METRES = 5;

const pinGalleryUploadPath = (slug: string) => `/dashboard/map/pin/${slug}/gallery/`;
const pinGalleryImagePath = (slug: string, imageId: number) => `/dashboard/map/pin/${slug}/gallery/${imageId}/`;
const pinGalleryJsonPath = (slug: string) => `/dashboard/map/pin/${slug}/gallery/json/`;

/** A point near `origin`, close enough to stay well inside a parcel this size and on-screen at the page's default zoom. */
function offsetCoordinate(origin: Coordinate, metresNorth: number, metresEast: number): Coordinate {
    const metresPerDegreeLat = 111_132.92 - 559.82 * Math.cos((2 * origin.latitude * Math.PI) / 180);
    const metresPerDegreeLon = 111_412.84 * Math.cos((origin.latitude * Math.PI) / 180);
    return {
        label: `${origin.label} +${metresNorth}mN +${metresEast}mE`,
        latitude: origin.latitude + metresNorth / metresPerDegreeLat,
        longitude: origin.longitude + metresEast / metresPerDegreeLon,
    };
}

/** The map's own projection, minimally - enough to convert a coordinate to a drop pixel and back. */
interface MinimalLeafletMap {
    latLngToContainerPoint(latlng: { lat: number; lng: number }): { x: number; y: number };
    containerPointToLatLng(point: [number, number]): { lat: number; lng: number };
}

/**
 * Wraps `L.Map.prototype.initialize` so the page's own Leaflet instance ends up on `window.__hrshMaps`.
 * See docs note "reach the Leaflet map from Playwright" - no registry exists otherwise.
 */
async function installLeafletMapCapture(page: Page): Promise<void> {
    await page.addInitScript(() => {
        const w = window as unknown as { L?: { Map?: { prototype?: { initialize?: (...args: unknown[]) => unknown } } }; __hrshMaps?: unknown[] };
        const iv = window.setInterval(() => {
            const proto = w.L?.Map?.prototype;
            if (!proto?.initialize) return; // Leaflet loads after this script runs.
            window.clearInterval(iv);
            const original = proto.initialize;
            proto.initialize = function (this: unknown, ...args: unknown[]) {
                const result = original.apply(this, args);
                (w.__hrshMaps ??= []).push(this);
                return result;
            };
        }, 10);
    });
}

async function waitForMapInstance(page: Page): Promise<void> {
    await page.waitForFunction(() => ((window as unknown as { __hrshMaps?: unknown[] }).__hrshMaps?.length ?? 0) > 0, undefined, { timeout: 30_000 });
}

async function mapContainerPoint(page: Page, latlng: Coordinate): Promise<{ x: number; y: number }> {
    return page.evaluate((ll) => {
        const map = (window as unknown as { __hrshMaps: MinimalLeafletMap[] }).__hrshMaps[0]!;
        const point = map.latLngToContainerPoint({ lat: ll.latitude, lng: ll.longitude });
        return { x: Math.round(point.x), y: Math.round(point.y) };
    }, latlng);
}

async function mapLatLngAt(page: Page, point: { x: number; y: number }): Promise<{ lat: number; lng: number }> {
    return page.evaluate((p) => (window as unknown as { __hrshMaps: MinimalLeafletMap[] }).__hrshMaps[0]!.containerPointToLatLng([p.x, p.y]), point);
}

/**
 * Drives the native drop sequence directly, sharing one `DataTransfer` across the events - the
 * fallback for when Chromium's synthesized pointer drag (`locator.dragTo`) does not reach the
 * `text/photoid` handlers in `map-annotations.ts`.
 */
async function dispatchPhotoDrop(page: Page, imageId: number, point: { x: number; y: number }): Promise<void> {
    await page.evaluate(
        ({ imageId, point }) => {
            const source = document.querySelector<HTMLElement>(`li.photo-panel-item[data-id="${imageId}"]`);
            const target = document.querySelector<HTMLElement>("#map");
            if (!source || !target) {
                throw new Error("drag source or #map target not found in the DOM for the synthetic drop");
            }
            const dataTransfer = new DataTransfer();
            const fire = (type: string, el: HTMLElement, clientX: number, clientY: number) => {
                el.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, clientX, clientY, dataTransfer }));
            };
            const sourceRect = source.getBoundingClientRect();
            const targetRect = target.getBoundingClientRect();
            const dropX = targetRect.left + point.x;
            const dropY = targetRect.top + point.y;
            fire("dragstart", source, sourceRect.left + 1, sourceRect.top + 1);
            fire("dragenter", target, dropX, dropY);
            fire("dragover", target, dropX, dropY);
            fire("drop", target, dropX, dropY);
            fire("dragend", source, dropX, dropY);
        },
        { imageId, point },
    );
}

interface GalleryImageEntry {
    id: number;
    latitude: number | null;
    longitude: number | null;
    map_hidden: boolean;
}

async function readGalleryImage(page: Page, slug: string, imageId: number): Promise<GalleryImageEntry | null> {
    const response = await page.request.get(pinGalleryJsonPath(slug));
    if (!response.ok()) {
        throw new Error(`${pinGalleryJsonPath(slug)} answered HTTP ${response.status()}`);
    }
    const body = (await response.json()) as { images: GalleryImageEntry[] };
    return body.images.find((entry) => entry.id === imageId) ?? null;
}

/** Deletes the test's own upload through the same endpoint the page's own delete button calls, so a rerun starts clean. */
async function deleteUploadedPhoto(page: Page, slug: string, imageId: number): Promise<number> {
    return page.evaluate(
        async ({ url }) => {
            const token = (window as unknown as { CSRF_TOKEN?: string }).CSRF_TOKEN ?? "";
            const response = await fetch(url, { method: "DELETE", headers: { "X-CSRFToken": token } });
            return response.status;
        },
        { url: pinGalleryImagePath(slug, imageId) },
    );
}

async function openMineGallery(page: Page): Promise<void> {
    await waitForHtmxSettled(page, 30_000);
    // The Buildings card has a "Mine" tab too.
    await page.getByLabel("Media view").getByRole("tab", { name: "Mine", exact: true }).click();
}

async function openPhotosSidebar(page: Page): Promise<void> {
    // On the pin page the sidebar is an off-canvas drawer, opened by its handle.
    const drawer = page.locator("#detail-pin-list-panel");
    if (!(await drawer.evaluate((panel) => panel.classList.contains("open")))) {
        await page.locator("#detail-pin-list-handle").click();
    }
    await page.locator("#map-panel-tab-photos").click();
    await expect(page.locator("#map-panel-photos")).toBeVisible();
}

test.describe("Hudson River State Hospital - placing a photo by dragging it onto the map", () => {
    test.beforeEach(async ({ guard }) => {
        for (const host of THIRD_PARTY_TILE_HOSTS) {
            guard.allow(host);
        }
    });

    test("the private pin page's Mine gallery offers a file input to upload photos through", async ({ campus, page }) => {
        await page.goto(pinDetail(campus.pin.slug), { waitUntil: "domcontentloaded" });
        await openMineGallery(page);

        await expect(
            page.locator("#gallery-file-input"),
            "no #gallery-file-input exists after switching to the Media card's \"Mine\" tab - the upload flow this suite drives has nowhere to attach a file",
        ).toBeAttached();
    });

    test("uploading a photo with no GPS and dragging it onto the map sets its coordinates at the drop point", async ({ campus, page }) => {
        let uploadedImageId: number | null = null;
        let uploadedUrl = "";

        try {
            await installLeafletMapCapture(page);
            await test.step("open the private pin page and reveal the upload input", async () => {
                const response = await page.goto(pinDetail(campus.pin.slug), { waitUntil: "domcontentloaded" });
                expect(response?.ok(), `opening the pin page answered HTTP ${response?.status()}`).toBeTruthy();
                await waitForMapInstance(page);
                await openMineGallery(page);
                await expect(page.locator("#gallery-file-input")).toBeAttached();
            });

            const uploadStartedAt = Date.now();
            let tile!: Locator;
            await test.step("upload a photo with no GPS through the page's own file input", async () => {
                const fileInput = page.locator("#gallery-file-input");
                const [uploadResponse] = await Promise.all([
                    page.waitForResponse((res) => res.request().method() === "POST" && new URL(res.url()).pathname === pinGalleryUploadPath(campus.pin.slug), { timeout: 30_000 }),
                    fileInput.setInputFiles({ name: `hrsh-drop-${Date.now()}.jpg`, mimeType: "image/jpeg", buffer: uniqueTinyJpeg() }),
                ]);
                expect(uploadResponse.ok(), `the upload answered HTTP ${uploadResponse.status()}`).toBeTruthy();
                const body = (await uploadResponse.json()) as { id: number; url: string; latitude: number | null };
                expect(body.latitude, "the freshly uploaded photo already carries coordinates, so dragging it would not be testing placement at all").toBeNull();
                uploadedImageId = body.id;
                uploadedUrl = body.url;
                tile = page.locator(`li.photo-panel-item[data-id="${uploadedImageId}"]`);
            });

            await test.step("the upload appears in the map sidebar's Photos panel as unplaced, without a reload", async () => {
                await openPhotosSidebar(page);
                await expect(tile, "the uploaded photo never appeared as a panel tile in #photo-panel-list - the panel should update live, not only after a reload").toBeVisible();
                await expect(tile.locator(".photo-panel-coord-badge"), "the new tile's badge is not marked no-gps").toHaveClass(/no-gps/);
            });
            recordMetric({ name: "hrsh.photo_placement.upload_to_visible_seconds", value: Math.round((Date.now() - uploadStartedAt) / 100) / 10, unit: "s" });

            const target = offsetCoordinate(campus.origin, 30, 20);
            const point = await mapContainerPoint(page, target);
            const expectedLatLng = await mapLatLngAt(page, point);

            const dragStartedAt = Date.now();
            let repositionResponse: Response | null = null;
            await test.step("dragging the tile onto the map posts the drop's coordinates", async () => {
                const mapLocator = page.locator("#map");
                const matchesReposition = (res: Response) => res.request().method() === "POST" && new URL(res.url()).pathname === pinGalleryImagePath(campus.pin.slug, uploadedImageId!);

                const firstAttempt = page.waitForResponse(matchesReposition, { timeout: 4_000 }).catch(() => null);
                await tile.dragTo(mapLocator, { targetPosition: point }).catch(() => undefined);
                repositionResponse = await firstAttempt;

                if (!repositionResponse) {
                    // locator.dragTo() drives a real pointer gesture, which Chromium is not
                    // guaranteed to turn into native dragstart/dragover/drop for a custom
                    // text/photoid payload - fall back to dispatching those events directly.
                    const fallbackAttempt = page.waitForResponse(matchesReposition, { timeout: 10_000 }).catch(() => null);
                    await dispatchPhotoDrop(page, uploadedImageId!, point);
                    repositionResponse = await fallbackAttempt;
                }

                expect(
                    repositionResponse,
                    `no POST to ${pinGalleryImagePath(campus.pin.slug, uploadedImageId!)} followed the drag - neither locator.dragTo() nor a synthetic DragEvent sequence reached the ` +
                        "map's drop handler (map-annotations.ts, the #map \"drop\" listener keyed on dataTransfer.types.includes(\"text/photoid\"))",
                ).not.toBeNull();
                expect(repositionResponse!.ok(), `the reposition request answered HTTP ${repositionResponse!.status()}`).toBeTruthy();
            });
            recordMetric({ name: "hrsh.photo_placement.drop_to_marker_ms", value: Date.now() - dragStartedAt, unit: "ms" });

            await test.step("without a reload, the tile is marked placed and a marker appears on the map", async () => {
                await expect(tile.locator(".photo-panel-coord-badge"), "the badge did not flip to has-gps after the drop").toHaveClass(/has-gps/);
                await expect(
                    page.locator(`.photo-marker-img[src="${uploadedUrl}"]`),
                    "no .photo-marker-img for the uploaded photo rendered on the map after the drop",
                ).toBeVisible();
            });

            await test.step("the read-back gallery reports the drop's own coordinates, within a few metres", async () => {
                const entry = await readGalleryImage(page, campus.pin.slug, uploadedImageId!);
                expect(entry, `the uploaded image (id ${uploadedImageId}) is missing from ${pinGalleryJsonPath(campus.pin.slug)} after being placed`).not.toBeNull();
                expect(entry!.latitude, "gallery.json still reports latitude: null after a successful reposition").not.toBeNull();
                expect(entry!.map_hidden, "a freshly placed photo should not be map_hidden").toBe(false);

                const actual: Coordinate = { label: "placed photo", latitude: entry!.latitude!, longitude: entry!.longitude! };
                const expected: Coordinate = { label: "expected drop point", latitude: expectedLatLng.lat, longitude: expectedLatLng.lng };
                const errorMetres = metresBetween(expected, actual);
                recordMetric({ name: "hrsh.photo_placement.placement_error_metres", value: Math.round(errorMetres * 10) / 10, unit: "count" });

                expect(
                    errorMetres,
                    `the saved coordinate is ${errorMetres.toFixed(1)} m from the pixel the test dropped on (expected ~${expected.latitude.toFixed(6)},${expected.longitude.toFixed(6)}, ` +
                        `got ${actual.latitude.toFixed(6)},${actual.longitude.toFixed(6)}). Check map.containerPointToLatLng's client-coordinate math in the #map "drop" listener`,
                ).toBeLessThanOrEqual(PLACEMENT_TOLERANCE_METRES);
            });

            await test.step("the placement survives a reload", async () => {
                const reloadResponse = await page.reload({ waitUntil: "domcontentloaded" });
                expect(reloadResponse?.ok(), `reloading the pin page answered HTTP ${reloadResponse?.status()}`).toBeTruthy();
                await waitForHtmxSettled(page, 30_000);

                await expect(
                    page.locator(`.photo-marker-img[src="${uploadedUrl}"]`),
                    "the placed photo's marker is gone after a reload - the coordinate was not actually persisted server-side",
                ).toBeVisible({ timeout: 15_000 });

                await openPhotosSidebar(page);
                await expect(
                    page.locator(`li.photo-panel-item[data-id="${uploadedImageId}"] .photo-panel-coord-badge`),
                    "the panel tile is has-gps no longer after a reload",
                ).toHaveClass(/has-gps/);
            });
        } finally {
            if (uploadedImageId !== null) {
                await waitForHtmxSettled(page, 30_000).catch(() => undefined);
                const status = await deleteUploadedPhoto(page, campus.pin.slug, uploadedImageId).catch(() => -1);
                if (status !== 204 && status !== 404) {
                    test.info().annotations.push({ type: "cleanup-failure", description: `DELETE ${pinGalleryImagePath(campus.pin.slug, uploadedImageId)} answered ${status}` });
                }
            }
        }
    });
});
