import { describe, expect, test } from "bun:test";

import { overviewIcon, overviewPopupHtml, type OverviewPoint } from "./pin-list-overview";

const BREAKOUT = '"><img src=x onerror=alert(1)>';

function point(overrides: Partial<OverviewPoint> = {}): OverviewPoint {
    return {
        uuid: "0b6c9a53-5e0b-4f5e-9d0e-3d2f1f6c0a11",
        name: "Mill",
        url: "/dashboard/map/pin/mill/",
        icon: "factory",
        color: "#aabbcc",
        rating: 3,
        address: "",
        description: "",
        last_visited: "Never",
        latitude: 40,
        longitude: -74,
        tags_data: [],
        ...overrides,
    };
}

function parse(html: string): HTMLElement {
    const host = document.createElement("div");
    host.innerHTML = html;
    return host;
}

describe("pin list overview markers", () => {
    test("a colour that is not a hex value cannot break out of the icon markup", () => {
        const host = parse(overviewIcon(point({ color: BREAKOUT }))?.html ?? "");
        expect(host.querySelector("img")).toBeNull();
        expect(host.querySelector(".map-pin-color-circle")).toBeNull();
    });

    test("a tag colour that is not a hex value cannot break out of the popup", () => {
        const host = parse(overviewPopupHtml(point({ tags_data: [{ name: "Tag", color: BREAKOUT }] })));
        expect(host.querySelector("img")).toBeNull();
        expect(host.querySelector(".popup-tag-chip")?.textContent).toBe("Tag");
    });

    test("a hex colour draws the tinted circle", () => {
        const icon = overviewIcon(point({ color: "#abc" }));
        expect(icon?.circled).toBe(true);
        const host = parse(icon?.html ?? "");
        const circle = host.querySelector<HTMLElement>(".map-pin-color-circle");
        expect(circle?.classList.contains("map-pin-color-circle--aabbcc")).toBe(true);
        expect(circle?.getAttribute("style")).toContain("rgba(170,187,204,0.8)");
    });

    test("a pin without an icon keeps Leaflet's default marker", () => {
        expect(overviewIcon(point({ icon: null }))).toBeNull();
    });

    test("text fields are escaped and the stars reflect the rating", () => {
        const host = parse(overviewPopupHtml(point({ name: "<b>x</b>", address: "<i>a</i>", last_visited: "2026-01-02" })));
        expect(host.querySelector(".popup-title")?.textContent).toBe("<b>x</b>");
        expect(host.querySelector("b, i:not(.material-icons)")).toBeNull();
        expect(host.querySelectorAll(".popup-star--on").length).toBe(3);
        expect(host.querySelector(".popup-visited")?.textContent).toContain("2026-01-02");
    });
});
