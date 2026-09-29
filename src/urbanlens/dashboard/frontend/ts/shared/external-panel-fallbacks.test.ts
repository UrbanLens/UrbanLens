import { beforeAll, beforeEach, describe, expect, mock, test } from "bun:test";

import { installExternalPanelFallbacks } from "./external-panel-fallbacks";

const flyAway = mock((_el: Element) => undefined);
const laterListener = mock(() => undefined);

beforeAll(() => {
    window.ulFlyToToolsFab = flyAway;
    installExternalPanelFallbacks();
    document.body.addEventListener("htmx:responseError", laterListener);
});

beforeEach(() => {
    flyAway.mockClear();
    laterListener.mockClear();
    document.body.innerHTML = `
        <div id="any-new-provider-section" data-ext-panel-204>Loading...</div>
        <div id="location-data-section" class="card-tabs">
            <button type="button" class="pin-plugin-tab-btn active" id="tab" hx-target="#location-data-body">Overview</button>
        </div>
        <div id="location-data-body">Loading...</div>`;
});

function fire(name: string, elt: Element, status: number): void {
    const xhr = { status, responseText: "" } as XMLHttpRequest;
    elt.dispatchEvent(new CustomEvent(name, { bubbles: true, detail: { elt, xhr } }));
}

describe("external panel fallbacks", () => {
    test("any panel carrying the marker is dismissed on a 204, with no list of ids to maintain", () => {
        const panel = document.getElementById("any-new-provider-section") as HTMLElement;
        fire("htmx:afterOnLoad", panel, 204);
        expect(flyAway).toHaveBeenCalledWith(panel);
    });

    test("a tab's 204 puts a message in the shared body instead of leaving the spinner", () => {
        fire("htmx:afterOnLoad", document.getElementById("tab") as HTMLElement, 204);
        expect(document.getElementById("location-data-body")?.textContent).toBe("No data available.");
    });

    test("a tab's server error is shown in place and not announced", () => {
        fire("htmx:responseError", document.getElementById("tab") as HTMLElement, 500);
        expect(document.getElementById("location-data-body")?.textContent).toBe("This data is temporarily unavailable.");
        expect(laterListener).not.toHaveBeenCalled();
    });

    test("a panel's server error is shown in place and not announced", () => {
        const panel = document.getElementById("any-new-provider-section") as HTMLElement;
        fire("htmx:responseError", panel, 500);
        expect(panel.textContent).toBe("External data temporarily unavailable.");
        expect(laterListener).not.toHaveBeenCalled();
    });
});
