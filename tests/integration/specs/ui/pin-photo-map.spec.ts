/**
 * Photos on the pin page's map (`entries/map-annotations.ts`, `shared/photo-map.ts`): opening, moving
 * and finding them. Pointer input goes through the real browser, because what broke here was the
 * element under the pointer, which no DOM-level test sees.
 */

import { type Locator, type Page, type Request } from "@playwright/test";

import { expect, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import { installLeafletMapCapture } from "../../lib/leaflet.js";
import { PinDetailPage } from "../../lib/pages/pin-detail-page.js";
import { csrfHeaders } from "../../lib/wiki.js";

type Point = { x: number; y: number };

/** The parts of the page's Leaflet objects these checks read. */
interface MarkerLike {
    _ulPhotoId?: number;
    getElement(): HTMLElement | undefined;
    getLatLng?: () => { lat: number; lng: number };
}
interface LayerLike {
    getLayers?: () => MarkerLike[];
    getVisibleParent?: (marker: MarkerLike) => MarkerLike | null;
}
interface MapLike {
    getContainer(): HTMLElement;
    setView(center: [number, number], zoom: number, options: { animate: boolean }): unknown;
    eachLayer(fn: (layer: LayerLike) => void): unknown;
    containerPointToLatLng(point: [number, number]): { lat: number; lng: number };
}

/** A 1x1 PNG with unique trailing bytes, so no rerun is deduped against an earlier upload. */
function uniquePng(marker: string): Buffer {
    const pixel = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "base64");
    return Buffer.concat([pixel, Buffer.from(`\n${marker}`, "utf-8")]);
}

/** Uploads one photo to the pin, optionally places it, and waits until it has a file to show. */
async function addPhoto(page: Page, slug: string, at?: { lat: number; lng: number }): Promise<number> {
    const base = `/dashboard/map/pin/${slug}/gallery`;
    const headers = await csrfHeaders(page);
    const response = await page.request.post(`${base}/`, {
        headers,
        multipart: { image: { name: "map-photo.png", mimeType: "image/png", buffer: uniquePng(resourceName(`map-photo ${Date.now()} ${Math.random()}`)) } },
    });
    test.skip(response.status() === 503, "the malware scanner is unavailable, so nothing can be uploaded");
    expect(response.ok(), `uploading answered ${response.status()}`).toBeTruthy();
    const id = ((await response.json()) as { id: number }).id;
    if (at) {
        const placed = await page.request.post(`${base}/${id}/`, { headers, data: { latitude: at.lat, longitude: at.lng } });
        expect(placed.ok(), `placing the photo answered ${placed.status()}`).toBeTruthy();
    }
    await expect
        .poll(async () => {
            const body = (await (await page.request.get(`/dashboard/vault/photos/processing/?ids=${id}`)).json()) as { items: Array<{ id: number; url: string }> };
            return !!body.items.find((img) => img.id === id)?.url;
        }, { message: "the upload never finished processing", timeout: 90_000 })
        .toBe(true);
    return id;
}

/** Adds a detail pin to the pin and returns its uuid. */
async function addDetailPin(page: Page, slug: string, at: { lat: number; lng: number }): Promise<string> {
    const response = await page.request.post(`/dashboard/map/pin/${slug}/detail-pins/`, {
        headers: await csrfHeaders(page),
        data: { name: resourceName("detail"), latitude: at.lat, longitude: at.lng, pin_type: "other" },
    });
    expect(response.ok(), `adding a detail pin answered ${response.status()}`).toBeTruthy();
    return ((await response.json()) as { uuid: string }).uuid;
}

/** Where the draggable marker standing at *at* is drawn, if one is drawn there on its own. */
async function markerAt(page: Page, at: { lat: number; lng: number }): Promise<Point | null> {
    return page.evaluate(({ lat, lng }) => {
        const maps = (window as unknown as { __ulLeafletMaps?: MapLike[] }).__ulLeafletMaps ?? [];
        const map = maps.find((m) => m.getContainer().id === "map");
        let element: HTMLElement | undefined;
        map?.eachLayer((layer) => {
            const marker = layer as unknown as MarkerLike;
            const pos = marker.getLatLng?.();
            const drawn = marker.getElement?.();
            // Coordinates are stored to six places.
            if (!element && pos && drawn?.classList.contains("leaflet-marker-draggable") && Math.abs(pos.lat - lat) < 2e-6 && Math.abs(pos.lng - lng) < 2e-6) element = drawn;
        });
        if (!element) return null;
        const rect = element.getBoundingClientRect();
        return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
    }, at);
}

/** Centres the pin map on a point at a zoom where the test's photos sit apart, then brings the map on screen. */
async function viewMap(page: Page, center: { lat: number; lng: number }, zoom = 19): Promise<void> {
    await page.locator("#map").scrollIntoViewIfNeeded();
    await page.evaluate(({ lat, lng, zoom }) => {
        const maps = (window as unknown as { __ulLeafletMaps?: MapLike[] }).__ulLeafletMaps ?? [];
        const map = maps.find((m) => m.getContainer().id === "map");
        if (!map) throw new Error("the pin map was not captured");
        map.setView([lat, lng], zoom, { animate: false });
    }, { ...center, zoom });
}

/** Where a photo is drawn on screen, and whether it is drawn on its own or inside a cluster. */
async function photoOnScreen(page: Page, imageId: number): Promise<(Point & { clustered: boolean }) | null> {
    return page.evaluate((imageId) => {
        const maps = (window as unknown as { __ulLeafletMaps?: MapLike[] }).__ulLeafletMaps ?? [];
        const map = maps.find((m) => m.getContainer().id === "map");
        if (!map) return null;
        let found: { marker: MarkerLike; group: LayerLike } | null = null;
        map.eachLayer((group) => {
            const marker = found ? undefined : group.getLayers?.().find((m) => m._ulPhotoId === imageId);
            if (marker) found = { marker, group };
        });
        if (!found) return null;
        const { marker, group } = found as { marker: MarkerLike; group: LayerLike };
        const drawn = group.getVisibleParent ? group.getVisibleParent(marker) : marker;
        const element = drawn?.getElement();
        if (!element) return null;
        const rect = element.getBoundingClientRect();
        return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, clustered: drawn !== marker };
    }, imageId);
}

async function waitForPhoto(page: Page, imageId: number): Promise<Point & { clustered: boolean }> {
    await expect.poll(() => photoOnScreen(page, imageId), { message: `photo ${imageId} is not drawn on the map` }).not.toBeNull();
    return (await photoOnScreen(page, imageId))!;
}

/** The map coordinate under a page point. */
async function latLngAt(page: Page, point: Point): Promise<{ lat: number; lng: number }> {
    return page.evaluate(({ x, y }) => {
        const maps = (window as unknown as { __ulLeafletMaps?: MapLike[] }).__ulLeafletMaps ?? [];
        const map = maps.find((m) => m.getContainer().id === "map")!;
        const rect = map.getContainer().getBoundingClientRect();
        const { lat, lng } = map.containerPointToLatLng([x - rect.left, y - rect.top]);
        return { lat, lng };
    }, point);
}

/**
 * Drags *source* onto a point of the map with the real mouse. Playwright's `dragTo` scrolls between grabbing and
 * dropping, which Chromium does not carry a drag across, so the page is made tall enough to show both at once.
 */
async function dragOntoMap(page: Page, source: Locator, target: Point): Promise<void> {
    await page.setViewportSize({ width: 1280, height: 2000 });
    await page.locator("#map").scrollIntoViewIfNeeded();
    const from = await source.boundingBox();
    const map = await page.locator("#map").boundingBox();
    expect(from && map && from.y + from.height <= 2000 && map.y >= 0, "the tile and the map do not fit on one screen").toBeTruthy();
    await page.mouse.move(from!.x + from!.width / 2, from!.y + from!.height / 2);
    await page.mouse.down();
    await page.mouse.move(from!.x + from!.width / 2 + 10, from!.y + from!.height / 2 + 10, { steps: 4 });
    await page.mouse.move(map!.x + target.x, map!.y + target.y, { steps: 12 });
    await page.mouse.up();
}

const isPlacement = (imageId: number) => (request: Request) => request.method() === "POST" && new URL(request.url()).pathname.endsWith(`/gallery/${imageId}/`);

/** Roughly two pixels at zoom 19. */
const PLACEMENT_TOLERANCE = 1e-5;

test.describe("photos on the pin map", () => {
    test.beforeEach(async ({ page }) => {
        await installLeafletMapCapture(page);
    });

    test("clicking a photo opens it in the photo lightbox, with its actions", async ({ page, api }) => {
        const pin = await api.createPin();
        const photo = await addPhoto(page, pin.slug, { lat: pin.latitude, lng: pin.longitude });
        await new PinDetailPage(page).goto(pin.slug);
        await viewMap(page, { lat: pin.latitude, lng: pin.longitude });

        const at = await waitForPhoto(page, photo);
        await page.mouse.move(at.x, at.y, { steps: 4 });
        await page.mouse.click(at.x, at.y);

        const lightbox = page.locator("#gallery-lightbox");
        await expect(lightbox).toHaveJSProperty("open", true);
        await expect(lightbox.locator("#lightbox-img")).toBeVisible();
        // Shown only for a photo the lightbox knows is a real row of the viewer's, as on the Photos tab.
        await expect(lightbox.locator("#lightbox-actions-menu")).toBeVisible();
    });

    test("resting the pointer on a photo leaves the same element under it", async ({ page, api }) => {
        const pin = await api.createPin();
        const photo = await addPhoto(page, pin.slug, { lat: pin.latitude, lng: pin.longitude });
        await new PinDetailPage(page).goto(pin.slug);
        await viewMap(page, { lat: pin.latitude, lng: pin.longitude });

        const at = await waitForPhoto(page, photo);
        await page.mouse.move(at.x, at.y, { steps: 4 });
        const under = await page.evaluateHandle(({ x, y }) => document.elementFromPoint(x, y), at);
        await page.waitForTimeout(500);
        expect(await under.evaluate((el) => el?.isConnected && el.classList.contains("photo-marker-img")), "the photo under the pointer was replaced").toBe(true);
        expect(await page.locator(".photo-marker.is-highlighted").count(), "hovering does not highlight the photo").toBe(1);
    });

    test("a photo dragged quickly lands where it is dropped", async ({ page, api }) => {
        const pin = await api.createPin();
        const photo = await addPhoto(page, pin.slug, { lat: pin.latitude, lng: pin.longitude });
        await new PinDetailPage(page).goto(pin.slug);
        await viewMap(page, { lat: pin.latitude, lng: pin.longitude });

        const from = await waitForPhoto(page, photo);
        const to = { x: from.x - 140, y: from.y - 90 };
        const expected = await latLngAt(page, to);
        const saved = page.waitForRequest(isPlacement(photo));
        await page.mouse.move(from.x, from.y, { steps: 2 });
        await page.mouse.down();
        await page.mouse.move(to.x, to.y, { steps: 3 });
        await page.mouse.up();

        const body = (await saved).postDataJSON() as { latitude: number; longitude: number };
        expect(Math.abs(body.latitude - expected.lat)).toBeLessThan(PLACEMENT_TOLERANCE);
        expect(Math.abs(body.longitude - expected.lng)).toBeLessThan(PLACEMENT_TOLERANCE);
    });

    test("one of your photos in the Media section can be dropped onto the map", async ({ page, api }) => {
        const pin = await api.createPin();
        const photo = await addPhoto(page, pin.slug);
        await new PinDetailPage(page).goto(pin.slug);
        await viewMap(page, { lat: pin.latitude, lng: pin.longitude });

        const tile = page.locator(`#media-gallery-grid .media-item[data-media-source="photos"][data-image-id="${photo}"]`);
        await expect(tile).toBeVisible();
        const saved = page.waitForRequest(isPlacement(photo));
        const target = { x: 300, y: 160 };
        await dragOntoMap(page, tile, target);
        const map = await page.locator("#map").boundingBox();
        const expected = await latLngAt(page, { x: map!.x + target.x, y: map!.y + target.y });

        const body = (await saved).postDataJSON() as { latitude: number; longitude: number };
        expect(Math.abs(body.latitude - expected.lat)).toBeLessThan(PLACEMENT_TOLERANCE);
        expect(Math.abs(body.longitude - expected.lng)).toBeLessThan(PLACEMENT_TOLERANCE);
        await waitForPhoto(page, photo);
    });

    test("a photo in the Media section's Mine view can be dropped onto the map", async ({ page, api }) => {
        const pin = await api.createPin();
        const photo = await addPhoto(page, pin.slug);
        await new PinDetailPage(page).goto(pin.slug);
        await viewMap(page, { lat: pin.latitude, lng: pin.longitude });

        await page.locator('#media-tabs .media-tab[data-tab="mine"]').click();
        const tile = page.locator(`#gallery-grid .gallery-item[data-id="${photo}"]`);
        await expect(tile).toBeVisible();
        const saved = page.waitForRequest(isPlacement(photo));
        await dragOntoMap(page, tile, { x: 300, y: 160 });

        const body = (await saved).postDataJSON() as { latitude: number; longitude: number };
        expect(Number.isFinite(body.latitude) && Number.isFinite(body.longitude)).toBe(true);
    });

    test("the map's side panel opens on Photos when there are no details to list", async ({ page, api }) => {
        const pin = await api.createPin();
        await addPhoto(page, pin.slug, { lat: pin.latitude, lng: pin.longitude });
        await new PinDetailPage(page).goto(pin.slug);
        await page.locator("#map").scrollIntoViewIfNeeded();
        await expect(page.locator("#detail-pin-list-ul > li")).toHaveCount(0);

        await page.locator("#detail-pin-list-handle").click();
        await expect(page.locator('.map-panel-tab[data-tab="photos"]')).toHaveClass(/is-active/);
        await expect(page.locator("#map-panel-photos .photo-panel-item")).toHaveCount(1);
        await expect(page.locator("#map-panel-details")).toBeHidden();
    });

    test("choosing a photo in the side panel marks it on the map without covering the map", async ({ page, api }) => {
        const pin = await api.createPin();
        const photo = await addPhoto(page, pin.slug, { lat: pin.latitude + 0.0002, lng: pin.longitude });
        await new PinDetailPage(page).goto(pin.slug);
        await viewMap(page, { lat: pin.latitude, lng: pin.longitude });
        await waitForPhoto(page, photo);

        await page.locator("#detail-pin-list-handle").click();
        await page.locator('.map-panel-tab[data-tab="photos"]').click();
        await page.locator(`#map-panel-photos .photo-panel-item[data-id="${photo}"]`).click();

        await expect(page.locator(".photo-marker.is-flashing")).toHaveCount(1);
        await expect(page.locator(".photo-marker.is-flashing .photo-marker-img")).toHaveCSS("animation-name", "photo-marker-flash");
        expect(await page.locator("#gallery-lightbox").evaluate((el) => (el as HTMLDialogElement).open)).toBe(false);
    });

    test("a photo chosen in the side panel comes to the top of the cluster it is in", async ({ page, api }) => {
        const pin = await api.createPin();
        const older = await addPhoto(page, pin.slug, { lat: pin.latitude, lng: pin.longitude });
        await addPhoto(page, pin.slug, { lat: pin.latitude, lng: pin.longitude });
        await new PinDetailPage(page).goto(pin.slug);
        await viewMap(page, { lat: pin.latitude, lng: pin.longitude });
        expect((await waitForPhoto(page, older)).clustered, "two photos at one spot were not clustered").toBe(true);

        const front = page.locator(".photo-cluster-icon .photo-cluster__img--front");
        const olderMarkerUrl = await page.evaluate(async (id) => {
            const body = (await (await fetch(location.pathname + "gallery/json/")).json()) as { images: Array<{ id: number; marker_thumb_url: string; url: string }> };
            const image = body.images.find((img) => img.id === id);
            return image?.marker_thumb_url || image?.url || "";
        }, older);
        await expect(front).not.toHaveAttribute("src", olderMarkerUrl);

        await page.locator("#detail-pin-list-handle").click();
        await page.locator('.map-panel-tab[data-tab="photos"]').click();
        await page.locator(`#map-panel-photos .photo-panel-item[data-id="${older}"]`).click();

        await expect(front).toHaveAttribute("src", olderMarkerUrl);
        await expect(page.locator(".photo-cluster-icon.is-flashing")).toHaveCount(1);
    });

    test("a detail pin dragged quickly lands where it is dropped", async ({ page, api }) => {
        const pin = await api.createPin();
        const at = { lat: pin.latitude + 0.0001, lng: pin.longitude + 0.0001 };
        const detail = await addDetailPin(page, pin.slug, at);
        await new PinDetailPage(page).goto(pin.slug);
        await viewMap(page, at);

        await expect.poll(() => markerAt(page, at), { message: "the detail pin is not drawn on the map" }).not.toBeNull();
        const from = (await markerAt(page, at))!;
        const to = { x: from.x - 140, y: from.y - 90 };
        const expected = await latLngAt(page, to);
        const saved = page.waitForRequest((request) => request.method() === "POST" && new URL(request.url()).pathname.endsWith(`/detail-pins/${detail}/`));
        await page.mouse.move(from.x, from.y, { steps: 2 });
        await page.mouse.down();
        await page.mouse.move(to.x, to.y, { steps: 3 });
        await page.mouse.up();

        const body = (await saved).postDataJSON() as { latitude: string; longitude: string };
        // The marker's anchor is its foot, not its centre, so the drop lands the grab offset away from the pointer.
        expect(Math.abs(Number(body.latitude) - expected.lat)).toBeLessThan(5e-5);
        expect(Math.abs(Number(body.longitude) - expected.lng)).toBeLessThan(5e-5);
    });

    test("the map's side panel opens on Details while it lists something", async ({ page, api }) => {
        const pin = await api.createPin();
        await addPhoto(page, pin.slug, { lat: pin.latitude, lng: pin.longitude });
        await addDetailPin(page, pin.slug, { lat: pin.latitude + 0.0001, lng: pin.longitude });
        await new PinDetailPage(page).goto(pin.slug);
        await page.locator("#map").scrollIntoViewIfNeeded();
        await expect(page.locator("#detail-pin-list-ul > li")).toHaveCount(1);

        await page.locator("#detail-pin-list-handle").click();
        await expect(page.locator('.map-panel-tab[data-tab="details"]')).toHaveClass(/is-active/);
        await expect(page.locator("#map-panel-photos")).toBeHidden();
    });
});
