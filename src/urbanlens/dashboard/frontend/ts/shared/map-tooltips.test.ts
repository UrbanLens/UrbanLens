/**
 * An area's hover label shows once the pointer rests on it, never follows the pointer, and stays shut while a map menu is open.
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";

import { bindAreaTooltip, unbindAreaTooltip } from "./map-tooltips";

type Handler = (event?: unknown) => void;

class FakeEvented {
    handlers = new Map<string, Handler[]>();

    on(types: string | Record<string, Handler>, fn?: Handler): this {
        const entries: [string, Handler][] = typeof types === "string" ? types.split(" ").map((type) => [type, fn!]) : Object.entries(types);
        for (const [type, handler] of entries) this.handlers.set(type, [...(this.handlers.get(type) ?? []), handler]);
        return this;
    }

    off(types: string | Record<string, Handler>, fn?: Handler): this {
        const entries: [string, Handler][] = typeof types === "string" ? types.split(" ").map((type) => [type, fn!]) : Object.entries(types);
        for (const [type, handler] of entries) this.handlers.set(type, (this.handlers.get(type) ?? []).filter((h) => h !== handler));
        return this;
    }

    fire(type: string, event?: unknown): void {
        for (const handler of this.handlers.get(type) ?? []) handler(event);
    }
}

class FakeLayer extends FakeEvented {
    open = false;
    openedAt: unknown = null;
    boundWith: Record<string, unknown> | null = null;

    bindTooltip(_content: string, options: Record<string, unknown>): this {
        this.boundWith = options;
        // Leaflet opens a permanent tooltip at once when the layer is already on a map.
        if (options.permanent) this.open = true;
        return this;
    }

    unbindTooltip(): this {
        this.boundWith = null;
        this.open = false;
        return this;
    }

    openTooltip(latlng?: unknown): this {
        this.open = true;
        this.openedAt = latlng;
        return this;
    }

    closeTooltip(): this {
        this.open = false;
        return this;
    }
}

const REST_MS = 15;
const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

let map: FakeEvented;
let layer: FakeLayer;

function bind(): void {
    bindAreaTooltip(map as never, layer as never, "Property boundary", { className: "boundary-tooltip" }, REST_MS);
}

beforeEach(() => {
    document.body.innerHTML = "";
    map = new FakeEvented();
    layer = new FakeLayer();
});

afterEach(() => {
    unbindAreaTooltip(layer as never);
});

describe("bindAreaTooltip", () => {
    test("binds shut, and opens only once the pointer rests", async () => {
        bind();
        expect(layer.open).toBe(false);

        layer.fire("mousemove", { latlng: [41.73, -73.93] });
        expect(layer.open).toBe(false);

        await sleep(REST_MS * 3);
        expect(layer.open).toBe(true);
        expect(layer.openedAt).toEqual([41.73, -73.93]);
    });

    test("does not follow the pointer: moving hides it and starts the wait again", async () => {
        bind();
        layer.fire("mousemove", { latlng: [1, 1] });
        await sleep(REST_MS * 3);
        expect(layer.open).toBe(true);

        layer.fire("mousemove", { latlng: [2, 2] });
        expect(layer.open).toBe(false);
        await sleep(REST_MS * 3);
        expect(layer.openedAt).toEqual([2, 2]);
    });

    test("never opens while a map menu is open", async () => {
        bind();
        document.body.innerHTML = '<div class="map-context-menu"></div>';

        layer.fire("mousemove", { latlng: [1, 1] });
        await sleep(REST_MS * 3);

        expect(layer.open).toBe(false);
    });

    test("a click, a right-click, leaving the area or the map moving hides it", async () => {
        for (const [target, type] of [["layer", "click"], ["layer", "contextmenu"], ["layer", "mouseout"], ["map", "movestart"]] as const) {
            bind();
            layer.fire("mousemove", { latlng: [1, 1] });
            await sleep(REST_MS * 3);
            expect(layer.open).toBe(true);

            (target === "layer" ? layer : map).fire(type);

            expect(layer.open).toBe(false);
        }
    });

    test("a click before the wait is up cancels it", async () => {
        bind();
        layer.fire("mousemove", { latlng: [1, 1] });
        layer.fire("click");
        await sleep(REST_MS * 3);

        expect(layer.open).toBe(false);
    });

    test("rebinding replaces the old handlers, and unbinding removes them", async () => {
        bind();
        bind();
        expect(layer.handlers.get("mousemove")?.length).toBe(1);
        expect(map.handlers.get("movestart")?.length).toBe(1);

        unbindAreaTooltip(layer as never);
        expect(layer.handlers.get("mousemove")?.length).toBe(0);
        expect(map.handlers.get("movestart")?.length).toBe(0);
        expect(layer.boundWith).toBeNull();
    });
});
