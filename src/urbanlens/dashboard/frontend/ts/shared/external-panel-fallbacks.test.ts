import { beforeAll, beforeEach, describe, expect, mock, test } from "bun:test";

import { installExternalPanelFallbacks } from "./external-panel-fallbacks";

const flyAway = mock((_el: HTMLElement | null) => undefined);
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
        <div id="location-data-body">Loading...</div>
        <div id="property-records-section">
            <div class="card-tabs">
                <button type="button" class="pin-plugin-tab-btn active" id="overview-tab" hx-target="#property-records-body">Overview</button>
                <button type="button" class="pin-plugin-tab-btn" data-panel-key="property_records" hx-target="#property-records-body">Parcel</button>
            </div>
            <div id="property-records-body" class="pin-plugin-tab-body">
                <div id="pending-tab" data-ext-panel-204><div class="view-loading">Loading...</div></div>
            </div>
        </div>`;
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

    test("a tab still fetching that ends with nothing says so, rather than leaving the tab blank", () => {
        fire("htmx:afterOnLoad", document.getElementById("pending-tab") as HTMLElement, 204);
        expect(document.getElementById("property-records-body")?.textContent?.trim()).toBe("No data available.");
        expect(flyAway).not.toHaveBeenCalled();
    });

    test("a tab still fetching whose poll fails says the data is unavailable", () => {
        const pending = document.getElementById("pending-tab") as HTMLElement;
        pending.dispatchEvent(new CustomEvent("htmx:sendError", { bubbles: true, detail: { elt: pending } }));
        expect(document.getElementById("property-records-body")?.textContent?.trim()).toBe("This data is temporarily unavailable.");
        expect(flyAway).not.toHaveBeenCalled();
    });

    test("a tab still fetching whose poll errors says so in place", () => {
        fire("htmx:responseError", document.getElementById("pending-tab") as HTMLElement, 500);
        expect(document.getElementById("property-records-body")?.textContent?.trim()).toBe("This data is temporarily unavailable.");
        expect(laterListener).not.toHaveBeenCalled();
    });

    test("an Overview naming empty tabs removes them from its own card", () => {
        document.body.dispatchEvent(new CustomEvent("pinTabsEmpty", { detail: { keys: ["property_records"] } }));
        expect(document.querySelector('[data-panel-key="property_records"]')).toBeNull();
        expect(document.getElementById("overview-tab")).not.toBeNull();
    });
});
