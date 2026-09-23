/**
 * Every map on the site has the same controls as the main map, where they apply (GOALS "Discovery:
 * browse and search"). The reference set is read off the main map: Leaflet's zoom control, the
 * shared layers component offering the street/terrain/satellite bases, the screenshot tool and the
 * jump-to-location search bar. Controls are looked for in the map's own wrapper, where the shared
 * `{% map_toolbar %}` / `{% map_layers_panel %}` / `{% map_search_bar %}` components render.
 */

import type { Page } from "@playwright/test";

import { expect, ifSharingPair, test } from "../../lib/fixtures.js";
import { resourceName } from "../../lib/env.js";
import { ensureFriends } from "../../lib/friendship.js";
import { appRoutes, pinDetail } from "../../lib/routes.js";
import type { ApiClient } from "../../lib/api-client.js";

interface ControlProfile {
    zoom: boolean;
    layers: boolean;
    bases: string[];
    screenshot: boolean;
    search: boolean;
}

const CORE_BASES = ["street", "terrain", "satellite"];

const GOALS_MAPS =
    'GOALS: "Every map on the site has the same controls, layout, and behavior (where they apply) - no page should require re-learning the map."';

/** Waits for Leaflet to own `mapSelector`, then reads which reference controls its wrapper carries. */
async function readControls(page: Page, mapSelector: string): Promise<ControlProfile> {
    const map = page.locator(mapSelector);
    await expect(map, `${mapSelector} never became a Leaflet map`).toHaveClass(/leaflet-container/);
    const scope = map.locator("xpath=..");
    const panel = scope.locator("[data-map-layers-panel]");
    return {
        zoom: (await map.locator(".leaflet-control-zoom").count()) > 0,
        layers: (await panel.count()) > 0,
        bases: await panel.locator('[data-layer-kind="base"]').evaluateAll((buttons) => buttons.map((button) => button.getAttribute("data-map-layer") ?? "")),
        screenshot: (await scope.locator("#screenshot-map-button").count()) > 0,
        search: (await scope.locator(".addr-search-bar").count()) > 0,
    };
}

/** Human-readable list of the ways `actual` falls short of the reference set. */
function missingControls(actual: ControlProfile): string[] {
    const missing: string[] = [];
    if (!actual.zoom) missing.push("zoom control");
    if (!actual.layers) missing.push("layers panel");
    for (const base of CORE_BASES) {
        if (actual.layers && !actual.bases.includes(base)) missing.push(`${base} base layer`);
    }
    if (!actual.screenshot) missing.push("screenshot tool");
    if (!actual.search) missing.push("location search bar");
    return missing;
}

interface MapTarget {
    name: string;
    selector: string;
    /** Builds whatever the page needs and returns its path. */
    prepare: (api: ApiClient) => Promise<string>;
    /** A path that must not render this map. */
    missing: string;
    /** Where the source shows it diverging from the reference set. */
    divergence: string;
}

async function listWithPin(api: ApiClient): Promise<string> {
    const pin = await api.createPin({ name: resourceName("map-controls list pin") });
    const list = await api.json<{ slug: string }>("post", "lists/", { name: resourceName(`map-controls list ${Date.now()}`) });
    api.track("list", list.slug, () => api.delete(`lists/${list.slug}/`));
    await api.json("post", `lists/${list.slug}/items/`, { pin_uuids: [pin.uuid] });
    return `/dashboard/lists/${list.slug}/`;
}

async function savedFilter(api: ApiClient): Promise<string> {
    const token = `mc${Math.random().toString(36).slice(2, 10)}`;
    await api.createPin({ name: resourceName(`${token} filter pin`) });
    const filter = await api.json<{ uuid: string }>("post", "saved-filters/", { name: resourceName(`${token} filter`), criteria: { name: token } });
    api.track("saved-filter", filter.uuid, () => api.delete(`saved-filters/${filter.uuid}/`));
    return `/dashboard/saved-filters/${filter.uuid}/`;
}

async function tripWithActivity(api: ApiClient): Promise<string> {
    const day = new Date(Date.now() + 2 * 86_400_000).toISOString().slice(0, 10);
    const trip = await api.json<{ slug: string }>("post", "trips/", { name: resourceName("map-controls trip"), start_date: day });
    api.track("trip", trip.slug, () => api.delete(`trips/${trip.slug}/`));
    const pin = await api.createPin({ name: resourceName("map-controls trip stop") });
    await api.json("post", `trips/${trip.slug}/activities/`, { title: resourceName("map-controls stop"), pin_slug: pin.slug, scheduled_at: `${day}T14:00:00Z` });
    return `/dashboard/trips/${trip.slug}/`;
}

const TARGETS: MapTarget[] = [
    {
        name: "the pin detail map",
        selector: "#map",
        prepare: async (api) => pinDetail((await api.createPin({ name: resourceName("map-controls pin") })).slug),
        missing: pinDetail("e2e-missing-pin"),
        divergence: "templates/dashboard/partials/layout/_map_annotations_panels.html:33-43 renders no {% map_search_bar %}",
    },
    {
        name: "the list detail map",
        selector: "#pin-list-map",
        prepare: listWithPin,
        missing: "/dashboard/lists/e2e-missing-list/",
        divergence: "templates/dashboard/pages/pin_lists/detail.html:89-94 renders no {% map_search_bar %} in the map section",
    },
    {
        name: "the saved filter preview map",
        selector: "#saved-filter-preview-map",
        prepare: savedFilter,
        missing: "/dashboard/saved-filters/00000000-0000-4000-8000-000000000000/",
        divergence: "templates/dashboard/pages/pin_lists/saved_filter_detail.html:54-58 renders no {% map_search_bar %}",
    },
    {
        name: "the trip map",
        selector: "#trip-map",
        prepare: tripWithActivity,
        missing: "/dashboard/trips/e2e-missing-trip/",
        divergence: "templates/dashboard/pages/trips/detail.html:36-43 renders no {% map_search_bar %} in the map wrapper",
    },
    {
        name: "the memories map",
        selector: "#memories-map",
        prepare: async () => appRoutes.memories,
        missing: "/dashboard/memories-e2e-missing/",
        divergence: "templates/dashboard/pages/memories/index.html:108-114 renders no {% map_search_bar %}",
    },
    {
        name: "the floorplan editor map",
        selector: "#floorplan-map",
        prepare: async (api) => `${pinDetail((await api.createPin({ name: resourceName("map-controls floorplan") })).slug)}floorplan/`,
        missing: `${pinDetail("e2e-missing-pin")}floorplan/`,
        divergence:
            "templates/dashboard/pages/floorplans/editor.html:135 offers only satellite,street,underlay,grid, and the page renders no {% map_toolbar %} screenshot tool or {% map_search_bar %}",
    },
];

test.describe("map controls", () => {
    test("the main map exposes the reference control set", async ({ page }) => {
        await page.goto(appRoutes.map);
        const controls = await readControls(page, "#map");

        expect(missingControls(controls), "the main map is missing part of the set every other map is compared against").toEqual([]);
        // Leaflet's native layers control is not how the site switches layers; a reader that
        // reported it present would be reporting nothing.
        expect(await page.locator("#map .leaflet-control-layers").count(), "the main map also carries Leaflet's native layers control").toBe(0);
    });

    for (const target of TARGETS) {
        test(`${target.name} renders a Leaflet map`, async ({ page, api }) => {
            const path = await target.prepare(api);
            const response = await page.goto(path);
            expect(response?.status(), `${path} answered ${response?.status()}`).toBe(200);
            await expect(page.locator(target.selector)).toHaveClass(/leaflet-container/);
            expect((await page.request.get(target.missing)).status(), `${target.missing} names nothing and still rendered`).toBe(404);
        });

        test(`${target.name} has the same controls as the main map`, async ({ page, api }) => {
            test.fail();
            test.info().annotations.push({ type: "goals-conflict", description: `${GOALS_MAPS} vs ${target.divergence}` });
            await page.goto(await target.prepare(api));
            const missing = missingControls(await readControls(page, target.selector));
            expect(missing, `${target.name} lacks: ${missing.join(", ")}`).toEqual([]);
        });
    }

    ifSharingPair()("the places-in-common map renders a Leaflet map", async ({ sharerApi, shareeApi, sharerPage }) => {
        const path = await commonPins(sharerApi, shareeApi);
        const response = await sharerPage.goto(path);
        expect(response?.status(), `${path} answered ${response?.status()}`).toBe(200);
        await expect(sharerPage.locator("#common-pins-map")).toHaveClass(/leaflet-container/);
        expect((await sharerPage.request.get("/dashboard/profile/e2e-missing-profile/common-pins/")).status()).toBe(404);
    });

    ifSharingPair()("the places-in-common map has the same controls as the main map", async ({ sharerApi, shareeApi, sharerPage }) => {
        test.fail();
        test.info().annotations.push({
            type: "goals-conflict",
            description: `${GOALS_MAPS} vs templates/dashboard/pages/profile/common_pins.html:21-25 renders no {% map_search_bar %}`,
        });
        await sharerPage.goto(await commonPins(sharerApi, shareeApi));
        const missing = missingControls(await readControls(sharerPage, "#common-pins-map"));
        expect(missing, `the places-in-common map lacks: ${missing.join(", ")}`).toEqual([]);
    });
});

/** Both accounts pin the same fresh coordinates, so the page has a map to show. */
async function commonPins(sharerApi: ApiClient, shareeApi: ApiClient): Promise<string> {
    const { b: sharee } = await ensureFriends(sharerApi, shareeApi);
    const latitude = 42.6526 + (Math.random() * 2 - 1) * 0.4;
    const longitude = -73.7562 + (Math.random() * 2 - 1) * 0.4;
    await sharerApi.createPin({ name: resourceName("map-controls common"), latitude, longitude });
    await shareeApi.createPin({ name: resourceName("map-controls common"), latitude, longitude });
    return `/dashboard/profile/${sharee.slug}/common-pins/`;
}
