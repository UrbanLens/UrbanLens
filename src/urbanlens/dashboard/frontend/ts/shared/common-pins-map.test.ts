import { expect, test } from "bun:test";

import { commonPinPoints, commonPinPopup } from "./common-pins-map";

test("only well-formed pins with a position become points", () => {
    const raw = [
        { latitude: 42.1, longitude: -71.2, name: "Mill", url: "/dashboard/pins/mill/" },
        { latitude: "42", longitude: -71, name: "string lat", url: "/x/" },
        { latitude: 91, longitude: 0, name: "off the globe", url: "/x/" },
        { latitude: 1, longitude: 2 },
        null,
    ];
    expect(commonPinPoints(raw)).toEqual([
        { latitude: 42.1, longitude: -71.2, name: "Mill", url: "/dashboard/pins/mill/" },
        { latitude: 1, longitude: 2, name: "", url: "" },
    ]);
    expect(commonPinPoints({ not: "a list" })).toEqual([]);
});

test("a popup shows the name as text and links only to a page on this site", () => {
    const popup = commonPinPopup({ latitude: 0, longitude: 0, name: "<img src=x onerror=alert(1)>", url: "/dashboard/pins/a/" });
    expect(popup.querySelector(".popup-title")?.textContent).toBe("<img src=x onerror=alert(1)>");
    expect(popup.querySelector("img")).toBeNull();
    expect(popup.querySelector("a.view-full-pin")?.getAttribute("href")).toBe("/dashboard/pins/a/");
    for (const url of ["javascript:alert(1)", "//evil.example/", "https://evil.example/"]) {
        expect(commonPinPopup({ latitude: 0, longitude: 0, name: "x", url }).querySelector("a")).toBeNull();
    }
});
