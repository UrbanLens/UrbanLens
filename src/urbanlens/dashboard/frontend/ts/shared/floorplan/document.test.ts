import { describe, expect, test } from "bun:test";

import { type Floor, copyFloorContents, newConnectorId, nextLocalId } from "./document";

function floorWithContents(): Floor {
    return {
        uuid: "floor-1",
        level: 0,
        name: "Ground floor",
        walls: [
            {
                uuid: "wall-1",
                kind: "exterior",
                thickness: "normal",
                ax: 0,
                ay: 0,
                bx: 4,
                by: 0,
                openings: [
                    {
                        uuid: "opening-1",
                        kind: "door",
                        t_start: 0.4,
                        t_end: 0.6,
                        swing: "left",
                        locks: [{ uuid: "lock-1", name: "padlock", state: "locked" }],
                    },
                ],
            },
            { uuid: "wall-2", kind: "interior", thickness: "thin", ax: 4, ay: 0, bx: 4, by: 3, openings: [] },
        ],
        rooms: [{ uuid: "room-1", name: "Boiler room", x: 2, y: 1 }],
        markers: [
            { uuid: "marker-1", kind: "stair", name: "West stair", x: 1, y: 1, connector_id: "shaft-a", lat: 41.7, lng: -73.9 },
            { uuid: "marker-2", kind: "hazard", name: "Hole", x: 3, y: 2 },
        ],
    };
}

/** Every uuid appearing anywhere in a copied result. */
function uuidsOf(copy: ReturnType<typeof copyFloorContents>): string[] {
    const ids: string[] = [];
    for (const wall of copy.walls) {
        ids.push(wall.uuid as string);
        for (const opening of wall.openings) {
            ids.push(opening.uuid as string);
            for (const lock of opening.locks ?? []) ids.push(lock.uuid as string);
        }
    }
    for (const room of copy.rooms) ids.push(room.uuid as string);
    for (const marker of copy.markers) ids.push(marker.uuid as string);
    return ids;
}

describe("copyFloorContents", () => {
    test("copies the walls and their openings", () => {
        const copy = copyFloorContents(floorWithContents());

        expect(copy.walls).toHaveLength(2);
        expect(copy.walls[0]?.openings).toHaveLength(1);
        expect(copy.walls[0]?.ax).toBe(0);
        expect(copy.walls[0]?.bx).toBe(4);
        expect(copy.walls[1]?.kind).toBe("interior");
    });

    test("no source uuid survives anywhere in the copy", () => {
        // The server matches items to rows by uuid and deletes by omission, so
        // a carried-over uuid would move the source floor's rows rather than
        // duplicate them - emptying the floor that was copied from.
        const source = floorWithContents();
        const copy = copyFloorContents(source, { rooms: true, markers: true, connectors: true });

        const sourceIds = new Set(["floor-1", "wall-1", "wall-2", "opening-1", "lock-1", "room-1", "marker-1", "marker-2"]);
        for (const id of uuidsOf(copy)) {
            expect(sourceIds.has(id)).toBe(false);
        }
    });

    test("every copied item is given an id, and they are all distinct", () => {
        const ids = uuidsOf(copyFloorContents(floorWithContents(), { rooms: true, markers: true }));

        expect(ids.every((id) => typeof id === "string" && id.length > 0)).toBe(true);
        expect(new Set(ids).size).toBe(ids.length);
    });

    test("the source floor is left untouched", () => {
        const source = floorWithContents();
        const before = structuredClone(source);

        copyFloorContents(source, { rooms: true, markers: true });

        expect(source).toEqual(before);
    });

    test("mutating the copy cannot reach back into the source", () => {
        const source = floorWithContents();
        const copy = copyFloorContents(source);

        (copy.walls[0] as { ax: number }).ax = 999;
        copy.walls[0]?.openings.push({ kind: "window", t_start: 0.1, t_end: 0.2, swing: "none" });

        expect(source.walls[0]?.ax).toBe(0);
        expect(source.walls[0]?.openings).toHaveLength(1);
    });

    test("a door's locks come across as new rows of their own", () => {
        const copy = copyFloorContents(floorWithContents());

        const locks = copy.walls[0]?.openings[0]?.locks ?? [];
        expect(locks).toHaveLength(1);
        expect(locks[0]?.name).toBe("padlock");
        expect(locks[0]?.state).toBe("locked");
        expect(locks[0]?.uuid).not.toBe("lock-1");
    });

    test("room names come across by default", () => {
        const copy = copyFloorContents(floorWithContents());

        expect(copy.rooms).toHaveLength(1);
        expect(copy.rooms[0]?.name).toBe("Boiler room");
    });

    test("markers stay behind by default", () => {
        expect(copyFloorContents(floorWithContents()).markers).toEqual([]);
    });

    test("copied markers drop their connector unless asked", () => {
        const copy = copyFloorContents(floorWithContents(), { markers: true });

        expect(copy.markers).toHaveLength(2);
        expect(copy.markers[0]?.connector_id).toBeNull();
    });

    test("connectors can be kept deliberately", () => {
        const copy = copyFloorContents(floorWithContents(), { markers: true, connectors: true });

        expect(copy.markers[0]?.connector_id).toBe("shaft-a");
    });

    test("copied markers drop the derived world coordinates", () => {
        // x/y is the source of truth; lat/lng is recomputed before each save.
        const copy = copyFloorContents(floorWithContents(), { markers: true });

        expect(copy.markers[0]?.lat).toBeUndefined();
        expect(copy.markers[0]?.lng).toBeUndefined();
        expect(copy.markers[0]?.x).toBe(1);
    });

    test("an empty floor copies to empty", () => {
        const copy = copyFloorContents({ level: 1, name: "First", walls: [], rooms: [], markers: [] }, { rooms: true, markers: true });

        expect(copy).toEqual({ walls: [], rooms: [], markers: [] });
    });
});

describe("newConnectorId", () => {
    test("two ids from the same session differ", () => {
        expect(newConnectorId()).not.toBe(newConnectorId());
    });

    test("ids are not the per-session counter", () => {
        // The counter restarts at one on every page load. That is fine for item
        // uuids, which the server replaces on save, and wrong for a connector
        // id, which is stored exactly as sent - two shafts drawn in two sessions
        // both came out "local-3" and read as one staircase.
        expect(newConnectorId()).not.toMatch(/^local-\d+$/);
        expect(nextLocalId()).toMatch(/^local-\d+$/);
    });

    test("an id fits the column that stores it", () => {
        // FloorplanMarker.connector_id is CharField(max_length=64).
        expect(newConnectorId().length).toBeLessThanOrEqual(64);
    });

    test("a run of ids is all distinct", () => {
        const ids = new Set(Array.from({ length: 500 }, () => newConnectorId()));
        expect(ids.size).toBe(500);
    });
});
