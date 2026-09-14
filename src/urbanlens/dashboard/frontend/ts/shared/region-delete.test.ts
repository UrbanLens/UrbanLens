/** Deleting a drawn region. Leaflet is not loaded here - the mode only talks to its group. */
import { describe, expect, test } from "bun:test";

import { ImmediateDeleteMode } from "./region-delete";

type ClickHandler = (e: { propagatedFrom?: object }) => void;

function fakeGroup(count: number) {
    const layers: object[] = Array.from({ length: count }, (_, i) => ({ id: i }));
    const handlers = new Set<ClickHandler>();
    const group = {
        getLayers: () => [...layers],
        removeLayer: (layer: object) => {
            const index = layers.indexOf(layer);
            if (index >= 0) layers.splice(index, 1);
            return group;
        },
        on: (_type: string, fn: ClickHandler) => {
            handlers.add(fn);
            return group;
        },
        off: (_type: string, fn: ClickHandler) => {
            handlers.delete(fn);
            return group;
        },
    };
    const click = (index: number) => {
        const layer = layers[index];
        if (!layer) throw new Error(`no region at ${index}`);
        handlers.forEach((fn) => fn({ propagatedFrom: layer }));
    };
    return { layers, click, group: group as unknown as ConstructorParameters<typeof ImmediateDeleteMode>[0] };
}

describe("ImmediateDeleteMode", () => {
    test("a click while armed removes the region and reports it at once", () => {
        const { layers, click, group } = fakeGroup(2);
        let deletions = 0;
        const mode = new ImmediateDeleteMode(group, () => {
            deletions += 1;
        });
        const first = layers[0];

        mode.arm();
        click(0);

        expect(layers).not.toContain(first);
        expect(layers).toHaveLength(1);
        expect(deletions).toBe(1);
    });

    test("disarming keeps the deletion rather than reverting it", () => {
        const { layers, click, group } = fakeGroup(2);
        const mode = new ImmediateDeleteMode(group, () => {});

        mode.arm();
        click(0);
        mode.disarm();

        expect(layers).toHaveLength(1);
    });

    test("a click while disarmed removes nothing", () => {
        const { layers, click, group } = fakeGroup(2);
        let deletions = 0;
        const mode = new ImmediateDeleteMode(group, () => {
            deletions += 1;
        });

        mode.toggle();
        mode.toggle();
        click(0);

        expect(layers).toHaveLength(2);
        expect(deletions).toBe(0);
    });

    test("arming a map with no regions does nothing", () => {
        const { group } = fakeGroup(0);
        const changes: boolean[] = [];
        const mode = new ImmediateDeleteMode(group, () => {}, (armed) => changes.push(armed));

        mode.arm();

        expect(mode.isArmed).toBe(false);
        expect(changes).toEqual([]);
    });

    test("deleting the last region disarms", () => {
        const { click, group } = fakeGroup(1);
        const changes: boolean[] = [];
        const mode = new ImmediateDeleteMode(group, () => {}, (armed) => changes.push(armed));

        mode.arm();
        click(0);

        expect(mode.isArmed).toBe(false);
        expect(changes).toEqual([true, false]);
    });
});
