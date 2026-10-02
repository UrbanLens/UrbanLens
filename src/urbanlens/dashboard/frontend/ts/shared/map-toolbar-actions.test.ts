import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";

import { installMapToolbarActions } from "./map-toolbar-actions";

type Globals = Record<string, unknown>;
const globals = window as unknown as Globals;
const NAMES = ["openAddPinDialog", "toggleFilterPanel", "_togglePinListPanel", "toggleSelectMode", "toggleDetailPinSelectMode", "toggleBuildingImportSelectMode", "_openMapScreenshot", "_openMapToolbarScreenshot", "map"];
const saved = new Map(NAMES.map((name) => [name, globals[name]]));
let calls: unknown[][] = [];

beforeAll(() => {
    installMapToolbarActions();
    installMapToolbarActions();
});

afterAll(() => {
    for (const [name, value] of saved) globals[name] = value;
});

beforeEach(() => {
    calls = [];
    for (const name of NAMES.slice(0, -2)) globals[name] = () => void calls.push([name]);
    globals._openMapToolbarScreenshot = (map: unknown, context: unknown) => void calls.push(["_openMapToolbarScreenshot", map, context]);
    globals.map = "the page's map";
});

function render(tool: string, extra = ""): HTMLElement {
    document.body.innerHTML = `<div class="map-buttons"><button type="button" class="map-btn-icon" data-map-tool="${tool}"${extra}><i id="icon">x</i></button></div>`;
    return document.getElementById("icon")!;
}

describe("the map toolbar's buttons", () => {
    test.each([
        ["add-pin", "openAddPinDialog"],
        ["toggle-filter-panel", "toggleFilterPanel"],
        ["toggle-pin-list", "_togglePinListPanel"],
        ["select-pins", "toggleSelectMode"],
        ["select-detail-pins", "toggleDetailPinSelectMode"],
        ["select-buildings", "toggleBuildingImportSelectMode"],
        ["pin-screenshot", "_openMapScreenshot"],
    ])("%s runs the page's %s", (tool, global) => {
        render(tool).click();
        expect(calls).toEqual([[global]]);
    });

    test("the screenshot opens the composer on the page's map with no context", () => {
        render("screenshot").click();
        expect(calls).toEqual([["_openMapToolbarScreenshot", "the page's map", null]]);
    });

    test("a trip's screenshot carries the trip's name", () => {
        render("screenshot", ` data-screenshot-context='{"tripName": "Ohio &lt;2026&gt;"}'`).click();
        expect(calls).toEqual([["_openMapToolbarScreenshot", "the page's map", { tripName: "Ohio <2026>" }]]);
    });

    test("a page without the function, or a tool nobody listed, does nothing", () => {
        delete globals.toggleFilterPanel;
        render("toggle-filter-panel").click();
        render("openAddPinDialog").click();
        expect(calls).toEqual([]);
    });
});
