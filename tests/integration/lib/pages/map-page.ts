/**
 * The map, which is the application's front door.
 *
 * Leaflet renders into a canvas of positioned tiles and markers, so almost
 * nothing here is assertable by text. What *is* assertable, and is what these
 * helpers expose, is the state the page reports about itself: that Leaflet
 * initialised at all, that the pins feed answered, and that a named pin came
 * back in it. Asserting on the feed rather than on a marker's pixels is both
 * stable and a sharper signal - a marker missing from the DOM and a pin missing
 * from the query are very different bugs.
 */

import { expect, type Locator, type Page } from "@playwright/test";

import { appRoutes, mapDataRoutes } from "../routes.js";

/** One pin as the map's own feed describes it. */
export interface MapFeedPin {
    uuid?: string;
    slug?: string;
    name?: string;
    latitude?: number;
    longitude?: number;
    [key: string]: unknown;
}

export class MapPage {
    readonly map: Locator;
    readonly filterPanel: Locator;
    readonly filterForm: Locator;
    readonly searchInput: Locator;
    readonly pinListPanel: Locator;
    readonly pinListBody: Locator;
    readonly pinListHandle: Locator;

    constructor(private readonly page: Page) {
        this.map = page.locator("#map");
        this.filterPanel = page.locator("#filter-panel");
        this.filterForm = page.locator("#filter-form");
        this.searchInput = page.locator("#fp-search");
        this.pinListPanel = page.locator("#pin-list-panel");
        this.pinListBody = page.locator("#pin-list-body");
        this.pinListHandle = page.locator("#pin-list-handle");
    }

    async goto(): Promise<void> {
        await this.page.goto(appRoutes.map);
        await this.expectMapReady();
    }

    /**
     * Waits until Leaflet has actually taken over the container.
     *
     * `#map` exists in the HTML whether or not the script ran, so its presence
     * proves nothing. Leaflet adds `.leaflet-container` and a tile pane when it
     * initialises, and that is the difference between a working map and a grey
     * rectangle - the most common visible symptom of a broken bundle.
     */
    async expectMapReady(): Promise<void> {
        await expect(this.map).toBeVisible();
        await expect(this.map).toHaveClass(/leaflet-container/);
        await expect(this.map.locator(".leaflet-map-pane")).toBeAttached();
    }

    /**
     * Fetches the map's own pin feed as the signed-in user.
     *
     * Uses the page's session rather than an API key, so this exercises the
     * internal, session-authenticated endpoint the map itself calls.
     */
    async fetchPins(): Promise<MapFeedPin[]> {
        const response = await this.page.request.get(mapDataRoutes.pins);
        expect(response.ok(), `${mapDataRoutes.pins} answered ${response.status()}`).toBeTruthy();
        const body = (await response.json()) as MapFeedPin[] | { pins?: MapFeedPin[] };
        return Array.isArray(body) ? body : (body.pins ?? []);
    }

    /** Opens the sidebar listing the pins in the current viewport. */
    async openPinList(): Promise<void> {
        if (!(await this.pinListPanel.evaluate((element) => element.classList.contains("open")))) {
            await this.pinListHandle.click();
        }
        await expect(this.pinListPanel).toHaveClass(/open/);
    }
}
