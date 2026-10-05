import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { handleMapArrival } from "./map-arrival";

const realToastr = window.toastr;
const realLocation = window.location;
let toasts: string[] = [];
let imports = 0;
let assigned: string[] = [];

beforeEach(() => {
    toasts = [];
    imports = 0;
    assigned = [];
    window.toastr = { ...realToastr, success: (message: string) => void toasts.push(message) } as typeof window.toastr;
    Object.defineProperty(window, "location", {
        value: { origin: "https://urbanlens.test", assign: (url: string) => void assigned.push(url) },
        configurable: true,
    });
    document.body.innerHTML = `
      <button id="import-pins-button" type="button">Import</button>
      <dialog id="pin-suggestions-intro-dialog"><button id="pin-suggestions-intro-accept" type="button">View suggestions</button></dialog>`;
    document.getElementById("import-pins-button")!.addEventListener("click", () => void imports++);
});

afterEach(() => {
    window.toastr = realToastr;
    Object.defineProperty(window, "location", { value: realLocation, configurable: true });
});

const intro = () => document.getElementById("pin-suggestions-intro-dialog") as HTMLDialogElement;
const QUIET = { showPinSuggestionsIntro: false, suggestionsUrl: "/dashboard/memories/locations/" };

describe("arriving at the map", () => {
    test("?import=1 opens the import dialog", () => {
        handleMapArrival("?import=1", QUIET);
        expect(imports).toBe(1);
        expect(intro().open).toBe(false);
    });

    test("a plain visit opens nothing and says nothing", () => {
        handleMapArrival("", QUIET);
        expect(imports).toBe(0);
        expect(toasts).toEqual([]);
    });

    test("a new user with suggestions is offered them, and accepting goes to the onboarding view", () => {
        handleMapArrival("", { ...QUIET, showPinSuggestionsIntro: true });
        expect(intro().open).toBe(true);
        document.getElementById("pin-suggestions-intro-accept")!.click();
        expect(assigned).toEqual(["/dashboard/memories/locations/?onboarding=1"]);
    });

    test("a suggestions URL on another site is not followed", () => {
        handleMapArrival("", { showPinSuggestionsIntro: true, suggestionsUrl: "https://elsewhere.test/phish/" });
        document.getElementById("pin-suggestions-intro-accept")!.click();
        expect(assigned).toEqual([]);
    });

    test("returning from accepting every suggestion confirms they are on the map", () => {
        handleMapArrival("?suggestions_imported=1", QUIET);
        expect(toasts).toEqual(["Your accepted pin suggestions are now on the map."]);
    });
});
