/**
 * The picker fetches its catalogue on first open, and must survive that going wrong.
 */

import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";

import { fillIconGrid, IconPicker, resetIconGridForTests } from "./icon-picker";

const GRID_URL = "/dashboard/ui/icon-picker-grid/?v=abc123";
// The real response's two fragments - the tab strip rides along with the
// catalogue it filters, so a picker costs one request rather than two.
const GRID_HTML =
    '<div data-icon-picker-tabs><button class="icon-tab" data-cat="things">Things</button></div>' +
    '<div data-icon-picker-items><button class="icon-picker-item" data-icon="camera" data-cat="things" data-label="camera"></button></div>';

const realFetch = globalThis.fetch;

function buildPage(...ids: string[]): void {
    document.body.innerHTML = ids
        .map(
            (id) => `
        <div class="icon-picker-dropdown" data-picker="${id}">
            <input type="hidden" id="icon-value-${id}" value="">
            <div id="icon-panel-${id}" class="icon-picker-panel" hidden>
                <input class="icon-picker-search-input">
                <div class="icon-picker-tabs" id="icon-tabs-${id}">
                    <button class="icon-tab active" data-cat=""></button>
                </div>
                <div class="icon-picker-grid" id="icon-grid-${id}" data-grid-url="${GRID_URL}">
                    <button class="icon-picker-item icon-picker-none" data-icon="" data-cat=""></button>
                </div>
            </div>
        </div>`,
        )
        .join("");
}

function grid(id: string): HTMLElement {
    return document.getElementById(`icon-grid-${id}`)!;
}

function okOnce(): Response {
    return new Response(GRID_HTML, { status: 200 });
}

/** Flush the fetch/`then` chain without counting microtasks by hand. */
async function settle(): Promise<void> {
    await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("lazily fetched icon grid", () => {
    beforeEach(() => {
        // One request is shared by every picker on the page, so the module holds it across calls.
        resetIconGridForTests();
    });

    afterEach(() => {
        globalThis.fetch = realFetch;
        document.body.innerHTML = "";
    });

    test("the catalogue is not in the page until a picker is opened", () => {
        globalThis.fetch = mock(async () => okOnce()) as unknown as typeof fetch;
        buildPage("a");

        expect(grid("a").querySelectorAll(".icon-picker-item").length).toBe(1);
    });

    test("opening a picker fills its grid", async () => {
        globalThis.fetch = mock(async () => okOnce()) as unknown as typeof fetch;
        buildPage("a");

        IconPicker.toggle("a");
        await settle();

        expect(grid("a").querySelector('[data-icon="camera"]')).not.toBeNull();
        expect(grid("a").dataset.iconsLoaded).toBe("1");
    });

    test("two pickers on one page cost one request", async () => {
        const fetcher = mock(async () => okOnce());
        globalThis.fetch = fetcher as unknown as typeof fetch;
        buildPage("a", "b");

        await fillIconGrid("a");
        await fillIconGrid("b");
        await settle();

        expect(fetcher.mock.calls.length).toBe(1);
        expect(grid("a").querySelector('[data-icon="camera"]')).not.toBeNull();
        expect(grid("b").querySelector('[data-icon="camera"]')).not.toBeNull();
    });

    test("reopening the same picker does not refetch or duplicate", async () => {
        const fetcher = mock(async () => okOnce());
        globalThis.fetch = fetcher as unknown as typeof fetch;
        buildPage("a");

        await fillIconGrid("a");
        await settle();
        await fillIconGrid("a");
        await settle();

        expect(fetcher.mock.calls.length).toBe(1);
        expect(grid("a").querySelectorAll('[data-icon="camera"]').length).toBe(1);
    });

    test("two opens racing the same response append one copy", async () => {
        const fetcher = mock(async () => okOnce());
        globalThis.fetch = fetcher as unknown as typeof fetch;
        buildPage("a");

        await Promise.all([fillIconGrid("a"), fillIconGrid("a")]);
        await settle();

        expect(grid("a").querySelectorAll('[data-icon="camera"]').length).toBe(1);
    });

    test("a failed fetch says so instead of leaving the panel blank", async () => {
        globalThis.fetch = mock(async () => new Response("nope", { status: 500 })) as unknown as typeof fetch;
        buildPage("a");

        await fillIconGrid("a");
        await settle();

        expect(grid("a").querySelector(".icon-picker-status")?.textContent).toContain("could not be loaded");
        expect(grid("a").dataset.iconsLoaded).toBeUndefined();
    });

    test("a failed fetch is retried on the next open", async () => {
        // The reason this is not `hx-trigger="click once"`: htmx spends `once`
        // when the event fires, so one failure is permanent until a page reload.
        let attempt = 0;
        const fetcher = mock(async () => {
            attempt += 1;
            return attempt === 1 ? new Response("nope", { status: 500 }) : okOnce();
        });
        globalThis.fetch = fetcher as unknown as typeof fetch;
        buildPage("a");

        await fillIconGrid("a");
        await settle();
        await fillIconGrid("a");
        await settle();

        expect(fetcher.mock.calls.length).toBe(2);
        expect(grid("a").querySelector('[data-icon="camera"]')).not.toBeNull();
        expect(grid("a").querySelector(".icon-picker-status")).toBeNull();
    });

    test("the current value is marked selected once the grid arrives", async () => {
        // The server used to render `selected` into the matching button.
        globalThis.fetch = mock(async () => okOnce()) as unknown as typeof fetch;
        buildPage("a");
        (document.getElementById("icon-value-a") as HTMLInputElement).value = "camera";

        await fillIconGrid("a");
        await settle();

        expect(grid("a").querySelector('[data-icon="camera"]')?.classList.contains("selected")).toBe(true);
    });

    test("a search typed while the grid loads still applies to it", async () => {
        let release: (value: Response) => void = () => {};
        globalThis.fetch = mock(
            () =>
                new Promise<Response>((resolve) => {
                    release = resolve;
                }),
        ) as unknown as typeof fetch;
        buildPage("a");

        IconPicker.toggle("a");
        document.querySelector<HTMLInputElement>(".icon-picker-search-input")!.value = "zzz";
        release(okOnce());
        await settle();

        expect(grid("a").querySelector<HTMLElement>('[data-icon="camera"]')?.style.display).toBe("none");
    });

    test("the category tabs arrive with the catalogue", async () => {
        globalThis.fetch = mock(async () => okOnce()) as unknown as typeof fetch;
        buildPage("a");

        await fillIconGrid("a");
        await settle();

        const tabs = document.getElementById("icon-tabs-a")!;
        expect(tabs.querySelector('[data-cat="things"]')).not.toBeNull();
        // "All" is rendered per picker because it is the tab that starts active.
        expect(tabs.querySelectorAll(".icon-tab").length).toBe(2);
    });

    test("a picker with no grid url is left alone", async () => {
        // Every other caller of this module renders the partial, but a template
        // that has not been updated must degrade to an empty grid, not a crash.
        const fetcher = mock(async () => okOnce());
        globalThis.fetch = fetcher as unknown as typeof fetch;
        buildPage("a");
        grid("a").removeAttribute("data-grid-url");

        await fillIconGrid("a");

        expect(fetcher.mock.calls.length).toBe(0);
    });
});
