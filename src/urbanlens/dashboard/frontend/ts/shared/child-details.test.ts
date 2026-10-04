/**
 * The page-wide "child pin details" setting, as the pin map adds it to its own requests and hears it change.
 */

import { describe, expect, test } from "bun:test";

import { CHILD_DETAILS_EVENT, childDetailsChange, readChildDetails, withChildDetails } from "./child-details";

describe("withChildDetails", () => {
    test("asks for the children, or leaves them out, explicitly", () => {
        expect(withChildDetails("/dashboard/map/pin/maple/markup/", true)).toBe("/dashboard/map/pin/maple/markup/?children=1");
        expect(withChildDetails("/dashboard/map/pin/maple/markup/", false)).toBe("/dashboard/map/pin/maple/markup/?children=0");
    });

    test("replaces a setting already in the URL and keeps its other parameters", () => {
        expect(withChildDetails("/gallery/?children=1&mine=1", false)).toBe("/gallery/?children=0&mine=1");
        expect(withChildDetails("/gallery/?mine=1", true)).toBe("/gallery/?mine=1&children=1");
    });

    test("leaves a URL alone when the page has no setting", () => {
        expect(withChildDetails("/wiki/markup/?children=1", null)).toBe("/wiki/markup/?children=1");
    });

    test("leaves an empty URL empty, so an unconfigured layer stays off", () => {
        expect(withChildDetails("", true)).toBe("");
    });

    test("keeps another origin's URL whole", () => {
        expect(withChildDetails("https://tiles.example.org/a.json", true)).toBe("https://tiles.example.org/a.json?children=1");
    });
});

describe("readChildDetails", () => {
    test("reads the page's setting, or none", () => {
        const config = document.createElement("div");
        expect(readChildDetails(config)).toBeNull();
        config.dataset.childDetails = "1";
        expect(readChildDetails(config)).toBe(true);
        config.dataset.childDetails = "0";
        expect(readChildDetails(config)).toBe(false);
    });
});

describe("childDetailsChange", () => {
    test("reads the new setting from the server's trigger", () => {
        expect(childDetailsChange(new CustomEvent(CHILD_DETAILS_EVENT, { detail: { include: true } }))).toBe(true);
        expect(childDetailsChange(new CustomEvent(CHILD_DETAILS_EVENT, { detail: { include: false } }))).toBe(false);
    });

    test("ignores an event that does not say", () => {
        expect(childDetailsChange(new CustomEvent(CHILD_DETAILS_EVENT, { detail: { value: "1" } }))).toBeNull();
        expect(childDetailsChange(new Event(CHILD_DETAILS_EVENT))).toBeNull();
    });
});
