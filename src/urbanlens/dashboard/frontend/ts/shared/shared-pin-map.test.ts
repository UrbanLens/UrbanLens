import { expect, test } from "bun:test";

import { installSharedPinMap, type SharedPinEngine, sharedPinView } from "./shared-pin-map";

function mapEl(attrs: string): HTMLElement {
    document.body.innerHTML = `<div id="shared-pin-map" ${attrs}></div><div id="shared-pin-map-layers"></div>`;
    const el = document.getElementById("shared-pin-map");
    if (!el) throw new Error("no map");
    return el;
}

test("a share with a location centres on it and names it", () => {
    expect(sharedPinView(mapEl('data-lat="42.65" data-lng="-73.76" data-name="Old &lt;b&gt;Mill&lt;/b&gt;"').dataset)).toEqual({
        center: [42.65, -73.76],
        zoom: 16,
        point: [42.65, -73.76],
        name: "Old <b>Mill</b>",
    });
});

test("a share without one still gets a map, of the continental US", () => {
    for (const attrs of ['data-lat="" data-lng=""', "", 'data-lat="nope" data-lng="1"']) {
        const view = sharedPinView(mapEl(attrs).dataset);
        expect(view.point).toBeNull();
        expect(view.zoom).toBe(4);
    }
});

test("the map is always built, with a marker only where there is a point", () => {
    const calls: string[] = [];
    const engine: SharedPinEngine = {
        createMap: (_el, view) => void calls.push(`map ${view.center.join(",")} z${view.zoom}`),
        addMarker: (point, name) => void calls.push(`marker ${point.join(",")} ${name}`),
    };
    installSharedPinMap(mapEl(""), engine);
    expect(calls).toEqual(["map 39.8283,-98.5795 z4"]);

    calls.length = 0;
    installSharedPinMap(mapEl('data-lat="1.5" data-lng="2.5" data-name="Mill"'), engine);
    expect(calls).toEqual(["map 1.5,2.5 z16", "marker 1.5,2.5 Mill"]);
});
